"""Photo observations remain revocable across history and concurrent requests."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Sequence
from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app.application.ports.llm import ToolAskAnswer, ToolExecutor, ToolSpec
from app.application.preferences.memory import forget_preference
from app.config.settings import Settings, get_settings
from app.infrastructure.cache.client import get_redis_client
from app.infrastructure.persistence.models.ask_conversation import (
    AskConversation,
    AskConversationState,
    AskMessage,
    AskStylingRevision,
)
from app.infrastructure.persistence.models.ask_session import AskSession
from app.infrastructure.persistence.models.preference_memory import UserPreferenceMemory
from app.infrastructure.persistence.models.style import UserStyleProfile
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.repositories.preference_memory_repo import SqlAlchemyPreferenceMemoryRepository
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import ask, style
from app.presentation.middlewares.exception_handler import setup_exception_handlers
from tests.api.test_designer_sessions import _setup

_ANALYSIS = {
    "source": "vision", "tags": ["minimal", "red", "black"],
    "style_tags": ["minimal"], "color_palette": ["red", "black"],
}


async def _reference(session: AsyncSession, user_id: uuid.UUID, *, queued: bool = False) -> uuid.UUID:
    source = UserStyleProfile(
        user_id=user_id, source_type="inspiration_image", image_url="https://cdn.example.com/reference.jpg",
        learn_status="queued" if queued else "learned", learned_summary=None if queued else _ANALYSIS,
    )
    session.add(source)
    await session.commit()
    assert isinstance(session.bind, AsyncConnection)
    await session.bind.commit()
    return source.id


def _deletion_app(session: AsyncSession, app: FastAPI, settings: Settings) -> FastAPI:
    assert isinstance(session.bind, AsyncConnection)
    sessions = async_sessionmaker(session.bind.engine, expire_on_commit=False)
    deletion = FastAPI()
    setup_exception_handlers(deletion)

    async def independent_session() -> AsyncIterator[AsyncSession]:
        async with sessions.begin() as transaction:
            yield transaction

    deletion.dependency_overrides[get_db_session] = independent_session
    deletion.dependency_overrides[get_settings] = lambda: settings
    deletion.dependency_overrides[get_redis_client] = app.dependency_overrides[get_redis_client]
    deletion.include_router(style.router)
    return deletion


async def _delete_reference(app: FastAPI, user_id: uuid.UUID, source_id: uuid.UUID) -> None:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://delete",
                           headers={"X-User-Id": str(user_id)}) as client:
        response = await client.delete(f"/v1/style/inspirations/{source_id}")
        assert response.status_code == 200, response.text


async def test_reference_answers_stay_out_of_recent_history_and_summary_after_deletion(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, user_id, _, model, settings = await _setup(db_session, monkeypatch)
    monkeypatch.setattr(ask.ask_uc, "_CONVERSATION_SUMMARY_THRESHOLD", 1)
    monkeypatch.setattr(ask.ask_uc, "_RECENT_MESSAGES_TO_KEEP", 2)
    source_id = await _reference(db_session, user_id)
    deletion = _deletion_app(db_session, app, settings)
    marker = "Synthetic copper detail inferred from the reference"
    original = model.ask_with_tools
    prompts: list[str] = []

    async def contextual_answer(
        system_prompt: str, question: str, tools: Sequence[ToolSpec], execute: ToolExecutor,
        *, json_mode: bool = False, max_rounds: int = 4,
    ) -> ToolAskAnswer:
        prompts.append(system_prompt)
        answer = await original(system_prompt, question, tools, execute, json_mode=json_mode, max_rounds=max_rounds)
        payload = json.loads(answer.text)
        payload.update(answer=marker, conversation_deltas={"applied_constraints": [marker]})
        return ToolAskAnswer(json.dumps(payload), answer.tokens_used)

    monkeypatch.setattr(model, "ask_with_tools", contextual_answer)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"X-User-Id": str(user_id)}) as client:
        first = await client.post("/v1/ask", json={
            "question": "Use this photo for a casual outfit", "inspiration_id": str(source_id),
            "request_id": str(uuid.uuid4()),
        })
        assert first.status_code == 200, first.text
        body = first.json()
        conversation_id = uuid.UUID(body["conversation_id"])
        state = await db_session.get(AskConversationState, conversation_id)
        assert state is not None and marker not in (state.active_constraints or [])
        assert state.last_outfit is not None and state.last_outfit["answer"] == ""
        assistant = await db_session.scalar(select(AskMessage).where(
            AskMessage.ask_session_id == uuid.UUID(body["ask_session_id"]), AskMessage.role == "assistant",
        ))
        assert assistant is not None and assistant.message_metadata["reference_context_used"] is True
        # Existing messages have only the source pointer in their saved brief.
        assistant.message_metadata = {
            key: value for key, value in assistant.message_metadata.items() if key != "reference_context_used"
        }
        await db_session.commit()
        second = await client.post("/v1/ask", json={
            "question": "Keep these trousers", "conversation_id": str(conversation_id),
            "expected_revision": body["design_brief"]["revision"], "request_id": str(uuid.uuid4()),
        })
        assert second.status_code == 200, second.text
        assert marker not in prompts[-1]
        assert marker not in (state.summary or "")
        state.summary = marker
        state.active_constraints = [marker]
        await db_session.commit()
        assert isinstance(db_session.bind, AsyncConnection)
        await db_session.bind.commit()
        await _delete_reference(deletion, user_id, source_id)
        model.clarify = True
        third = await client.post("/v1/ask", json={
            "question": "Continue with the current outfit", "conversation_id": str(conversation_id),
            "expected_revision": second.json()["design_brief"]["revision"], "request_id": str(uuid.uuid4()),
        })
        assert third.status_code == 200, third.text
        assert marker not in prompts[-1]
        assert "saved photo observations are currently unavailable" in prompts[-1]
        assert third.json()["styling"]["outfit"] == second.json()["styling"]["outfit"]
        assert third.json()["design_brief"]["constraints"]["inspiration_id"] == str(source_id)
    assert marker not in (state.summary or "") and marker not in (state.active_constraints or [])
    user = await db_session.get(User, user_id, populate_existing=True)
    assert user is not None and user.memory_context_reset_at is not None and user.memory_cleared_at is None


@pytest.mark.parametrize("operation", ["delete", "forget"])
async def test_reference_revoked_during_generation_cannot_save_answer_or_changed_brief(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    app, user_id, _, model, settings = await _setup(db_session, monkeypatch)
    source_id = await _reference(db_session, user_id)
    deletion = _deletion_app(db_session, app, settings)
    assert isinstance(db_session.bind, AsyncConnection)
    sessions = async_sessionmaker(db_session.bind.engine, expire_on_commit=False)

    async def revoke() -> None:
        if operation == "delete":
            await _delete_reference(deletion, user_id, source_id)
        else:
            async with sessions.begin() as transaction:
                await forget_preference(
                    SqlAlchemyPreferenceMemoryRepository(transaction), user_id=user_id,
                    preference_type="color", preference_value="blue",
                )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"X-User-Id": str(user_id)}) as client:
        first = await client.post("/v1/ask", json={
            "question": "Use this photo", "inspiration_id": str(source_id), "inspiration_only": True,
            "request_id": str(uuid.uuid4()),
        })
        assert first.status_code == 200, first.text
        body = first.json()
        await db_session.bind.commit()

        async def answer_while_revoked(
            system_prompt: str, question: str, tools: Sequence[ToolSpec], execute: ToolExecutor,
            *, json_mode: bool = False, max_rounds: int = 4,
        ) -> ToolAskAnswer:
            assert "Saved photo observations" in system_prompt
            updated = await execute("update_design_brief", {
                "evidence_text": question, "goal": "A goal inferred before revocation",
            })
            assert updated.content["updated"] is True
            await asyncio.wait_for(revoke(), timeout=5)
            return ToolAskAnswer(json.dumps({
                "answer": "An obsolete reference answer", "reply_kind": "general", "referenced_items": [],
                "stated_preferences": [{
                    "type": "color", "value": "blue", "polarity": "positive", "evidence_text": "I like blue",
                }],
            }), 5)

        monkeypatch.setattr(model, "ask_with_tools", answer_while_revoked)
        request_id = uuid.uuid4()
        second = await client.post("/v1/ask", json={
            "question": "I like blue; reconsider this outfit", "conversation_id": body["conversation_id"],
            "expected_revision": body["design_brief"]["revision"], "request_id": str(request_id),
        })
        assert second.status_code == 409, second.text
        recovered = await client.get(f"/v1/ask/conversations/{body['conversation_id']}")
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["design_brief"] == body["design_brief"]
        assert recovered.json()["styling"] == body["styling"]
    assert await db_session.scalar(select(AskSession.id).where(AskSession.client_request_id == request_id)) is None
    assert await db_session.scalar(select(func.count()).select_from(AskMessage)) == 2
    assert await db_session.scalar(select(func.count()).select_from(UserPreferenceMemory).where(
        UserPreferenceMemory.deleted_at.is_(None),
    )) == 0
    state = await db_session.get(AskConversationState, uuid.UUID(body["conversation_id"]), populate_existing=True)
    assert state is not None and state.summary is None


async def test_inline_visual_analysis_does_not_lock_the_source_before_the_revocation_fence(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, user_id, _, model, settings = await _setup(db_session, monkeypatch)
    source_id = await _reference(db_session, user_id, queued=True)
    deletion = _deletion_app(db_session, app, settings)
    monkeypatch.setattr(ask.ask_uc, "learn_from_image", AsyncMock(return_value=_ANALYSIS))

    async def delete_during_model(
        system_prompt: str, question: str, tools: Sequence[ToolSpec], execute: ToolExecutor,
        *, json_mode: bool = False, max_rounds: int = 4,
    ) -> ToolAskAnswer:
        assert "Saved photo observations" in system_prompt
        await asyncio.wait_for(_delete_reference(deletion, user_id, source_id), timeout=5)
        return ToolAskAnswer('{"answer":"An obsolete answer","reply_kind":"general","referenced_items":[]}', 4)

    monkeypatch.setattr(model, "ask_with_tools", delete_during_model)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"X-User-Id": str(user_id)}) as client:
        response = await client.post("/v1/ask", json={
            "question": "Design an outfit from this photo", "inspiration_id": str(source_id),
            "request_id": str(uuid.uuid4()),
        })
    assert response.status_code == 409, response.text
    assert await db_session.scalar(select(func.count()).select_from(UserStyleProfile)) == 0
    assert await db_session.scalar(select(func.count()).select_from(AskSession)) == 0
    assert await db_session.scalar(select(func.count()).select_from(AskStylingRevision)) == 0


@pytest.mark.parametrize(("locale", "explicit", "continued"), [
    (None, False, False), ("zh-CN", False, False),
    (None, True, False), ("zh-CN", True, False), ("zh-CN", False, True),
])
async def test_deleting_reference_only_clears_proven_initial_legacy_image_style(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
    locale: str | None, explicit: bool, continued: bool,
) -> None:
    app, user_id, _, model, settings = await _setup(db_session, monkeypatch)
    source_id = await _reference(db_session, user_id)
    deletion = _deletion_app(db_session, app, settings)
    request: dict[str, object] = {
        "question": "Use this reference for my outfit", "inspiration_id": str(source_id),
        "inspiration_only": True, "request_id": str(uuid.uuid4()),
    }
    if locale is not None:
        request["locale"] = locale
    if explicit:
        request["constraints"] = {"target_style": "minimal", "goal": "My explicit goal"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                           headers={"X-User-Id": str(user_id)}) as client:
        first = await client.post("/v1/ask", json=request)
        assert first.status_code == 200, first.text
        body = first.json()
        conversation_id = uuid.UUID(body["conversation_id"])
        if not explicit:
            assert body["design_brief"]["constraints"]["target_style"] is None
            # Recreate the old persisted representation while retaining its real request hash.
            conversation = await db_session.get(AskConversation, conversation_id)
            revision = await db_session.scalar(select(AskStylingRevision).where(
                AskStylingRevision.conversation_id == conversation_id,
            ))
            saved = await db_session.get(AskSession, uuid.UUID(body["ask_session_id"]))
            assert conversation is not None and revision is not None and saved is not None
            conversation.design_constraints = {**conversation.design_constraints, "target_style": "minimal"}
            revision.constraints = {**revision.constraints, "target_style": "minimal"}
            snapshot = saved.context["design_brief"]
            saved.context = {**saved.context, "design_brief": {
                **snapshot, "constraints": {**snapshot["constraints"], "target_style": "minimal"},
            }}
            await db_session.commit()
        if continued:
            model.clarify = True
            followup = await client.post("/v1/ask", json={
                "question": "Keep this style for now", "conversation_id": str(conversation_id),
                "expected_revision": body["design_brief"]["revision"], "request_id": str(uuid.uuid4()),
            })
            assert followup.status_code == 200, followup.text
            body = followup.json()
        assert isinstance(db_session.bind, AsyncConnection)
        await db_session.bind.commit()
        await _delete_reference(deletion, user_id, source_id)
        restored = await client.get(f"/v1/ask/conversations/{conversation_id}")
        assert restored.status_code == 200, restored.text
        brief = restored.json()["design_brief"]
        expected_style = "minimal" if explicit or continued else None
        assert brief["constraints"]["target_style"] == expected_style
        assert brief["constraints"]["goal"] == body["design_brief"]["constraints"]["goal"]
        # Saved outfit snapshots stay immutable; removing derived conditions advances the live brief only.
        assert restored.json()["styling"]["constraints"]["target_style"] == "minimal"
        assert brief["revision"] == body["design_brief"]["revision"] + int(not explicit and not continued)
        if not explicit and not continued:
            model.clarify = True
            following = await client.post("/v1/ask", json={
                "question": "Continue with the saved pieces", "conversation_id": str(conversation_id),
                "expected_revision": brief["revision"], "request_id": str(uuid.uuid4()),
            })
            assert following.status_code == 200, following.text
            assert model.seen_briefs[-1]["target_style"] == ""
