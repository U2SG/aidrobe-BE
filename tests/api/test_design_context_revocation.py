"""Revocable reference/experience context must not escape into persistent prose."""

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Sequence

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app.application.outfits import experiences
from app.application.ports.llm import ToolAskAnswer, ToolExecutor, ToolSpec
from app.config.settings import get_settings
from app.infrastructure.cache.client import get_redis_client
from app.infrastructure.persistence.models.ask_conversation import AskConversationState, AskMessage
from app.infrastructure.persistence.models.preference_memory import UserPreferenceMemory
from app.infrastructure.persistence.models.style import UserStyleProfile
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import ask, style, styling_experiences
from app.presentation.middlewares.exception_handler import setup_exception_handlers
from tests.api.test_ask_design_references import _reference
from tests.api.test_designer_sessions import _setup


@pytest.mark.parametrize("during_model", [False, True])
async def test_real_reference_delete_clears_context_and_fences_an_inflight_answer(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, during_model: bool,
) -> None:
    app, user_id, _, model, settings = await _setup(db_session, monkeypatch)
    settings.ENABLE_VISION_STYLE_LEARNING = True
    monkeypatch.setattr(style, "get_settings", lambda: settings)
    app.include_router(style.router)
    reference = await _reference(db_session, user_id)
    reference_id = reference.id
    assert isinstance(db_session.bind, AsyncConnection)
    sessions = async_sessionmaker(db_session.bind.engine, expire_on_commit=False)

    # A distinct request session makes deletion commit while the model request
    # still holds its old view of the reference, as on two real devices.
    delete_app = FastAPI()
    setup_exception_handlers(delete_app)

    async def independent_session() -> AsyncIterator[AsyncSession]:
        async with sessions() as session:
            async with session.begin():
                yield session

    delete_app.dependency_overrides[get_db_session] = independent_session
    delete_app.dependency_overrides[get_settings] = lambda: settings
    delete_app.dependency_overrides[get_redis_client] = app.dependency_overrides[get_redis_client]
    delete_app.include_router(style.router)
    headers = {"X-User-Id": str(user_id)}

    async def delete_reference() -> None:
        async with AsyncClient(transport=ASGITransport(app=delete_app), base_url="http://delete") as client:
            result = await client.delete(f"/v1/style/inspirations/{reference_id}", headers=headers)
            assert result.status_code == 200, result.text

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as client:
        first = await client.post("/v1/ask", json={
            "question": "用这张图帮我搭一套", "design_mode": True, "request_id": str(uuid.uuid4()),
            "constraints": {"reference_inspiration_id": str(reference_id)},
        })
        assert first.status_code == 200, first.text
        body = first.json()
        conversation_id = uuid.UUID(body["conversation_id"])
        message = await db_session.scalar(select(AskMessage).where(
            AskMessage.ask_session_id == uuid.UUID(body["ask_session_id"]), AskMessage.role == "assistant",
        ))
        assert message is not None and message.message_metadata["transient_design_context"] is True
        state = await db_session.get(AskConversationState, conversation_id)
        assert state is not None
        state.summary = "Old reference analysis that must not return"
        await db_session.commit()
        await db_session.bind.commit()

        if not during_model:
            await delete_reference()

        prompts: list[str] = []

        async def answer_while_reference_changes(
            system_prompt: str, question: str, tools: Sequence[ToolSpec], execute: ToolExecutor,
            *, json_mode: bool = False, max_rounds: int = 4,
        ) -> ToolAskAnswer:
            prompts.append(system_prompt)
            if during_model:
                assert "Selected inspiration's image analysis" in system_prompt
                updated = await execute("update_design_brief", {
                    "evidence_text": question, "goal": "A goal from the soon deleted image",
                    "notes": ["A condition from the soon deleted image"],
                })
                assert updated.content["updated"] is True
                await asyncio.wait_for(delete_reference(), timeout=5)
            else:
                assert "Selected inspiration's image analysis" not in system_prompt
                assert "Old reference analysis that must not return" not in system_prompt
            data: dict[str, object] = {
                "answer": "The deleted image must not turn into durable memory", "reply_kind": "general",
                "referenced_items": [],
            }
            if during_model:
                data["stated_preferences"] = [{
                    "type": "color", "value": "blue", "polarity": "positive", "evidence_text": "我喜欢蓝色",
                }]
            return ToolAskAnswer(json.dumps(data, ensure_ascii=False), 5)

        monkeypatch.setattr(model, "ask_with_tools", answer_while_reference_changes)
        second = await client.post("/v1/ask", json={
            "question": "我喜欢蓝色，再聊聊这套", "conversation_id": str(conversation_id),
            "request_id": str(uuid.uuid4()), "expected_revision": body["design_brief"]["revision"],
        })
        assert second.status_code == (409 if during_model else 200), second.text
        if during_model:
            recovered = await client.get(f"/v1/ask/conversations/{conversation_id}")
            assert recovered.status_code == 200, recovered.text
            assert recovered.json()["design_brief"] == body["design_brief"]
            assert recovered.json()["styling"]["outfit"]["outfit_id"] == body["styling"]["outfit"]["outfit_id"]

    assert len(prompts) == 1
    assert await db_session.scalar(select(func.count()).select_from(UserStyleProfile)) == 0
    assert await db_session.scalar(select(func.count()).select_from(UserPreferenceMemory)) == 0
    user = await db_session.scalar(select(User).where(User.id == user_id).execution_options(populate_existing=True))
    assert user is not None and user.memory_context_reset_at is not None and user.memory_cleared_at is None
    final_state = await db_session.scalar(select(AskConversationState).where(
        AskConversationState.conversation_id == conversation_id
    ).execution_options(populate_existing=True))
    assert final_state is not None and final_state.summary is None
    if during_model:
        assert "deleted image" not in json.dumps(final_state.last_outfit or {})


async def test_experience_model_deltas_stay_in_the_response_and_never_become_persistent_conditions(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, user_id, _, model, settings = await _setup(db_session, monkeypatch)
    monkeypatch.setattr(experiences, "get_settings", lambda: settings)
    monkeypatch.setattr(styling_experiences, "get_settings", lambda: settings)
    monkeypatch.setattr(ask.ask_uc, "_CONVERSATION_SUMMARY_THRESHOLD", 1)
    monkeypatch.setattr(ask.ask_uc, "_RECENT_MESSAGES_TO_KEEP", 1)
    app.include_router(styling_experiences.router)
    marker = "Copper colours were admired only once"
    headers = {"X-User-Id": str(user_id)}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test", headers=headers) as client:
        first = await client.post("/v1/ask", json={
            "question": "先搭一套", "design_mode": True, "request_id": str(uuid.uuid4()),
        })
        assert first.status_code == 200, first.text
        body = first.json()
        styling = body["styling"]
        conversation_id = uuid.UUID(body["conversation_id"])
        saved = await client.post("/v1/styling-experiences", json={
            "request_id": str(uuid.uuid4()), "outfit_id": styling["outfit"]["outfit_id"],
            "conversation_id": str(conversation_id), "revision": styling["revision"],
            "kind": "appreciated", "note": marker,
        })
        assert saved.status_code == 200, saved.text
        prompts: list[str] = []

        async def contextual_answer(
            system_prompt: str, question: str, tools: Sequence[ToolSpec], execute: ToolExecutor,
            *, json_mode: bool = False, max_rounds: int = 4,
        ) -> ToolAskAnswer:
            prompts.append(system_prompt)
            return ToolAskAnswer(json.dumps({
                "answer": marker, "reply_kind": "general", "referenced_items": [],
                "conversation_deltas": {"applied_constraints": [marker]},
            }), 5)

        monkeypatch.setattr(model, "ask_with_tools", contextual_answer)
        second = await client.post("/v1/ask", json={
            "question": "聊聊刚才的尝试", "conversation_id": str(conversation_id),
            "expected_revision": body["design_brief"]["revision"], "request_id": str(uuid.uuid4()),
        })
        assert second.status_code == 200, second.text
        assert marker in prompts[-1]
        assert marker in second.json()["deltas"]["applied_constraints"]
        state = await db_session.get(AskConversationState, conversation_id)
        assert state is not None and marker not in (state.active_constraints or [])
        assert state.last_outfit is not None and state.last_outfit["answer"] == ""

        deleted = await client.delete(f"/v1/styling-experiences/{saved.json()['experience_id']}")
        assert deleted.status_code == 200, deleted.text

        async def after_withdrawal(
            system_prompt: str, question: str, tools: Sequence[ToolSpec], execute: ToolExecutor,
            *, json_mode: bool = False, max_rounds: int = 4,
        ) -> ToolAskAnswer:
            assert marker not in system_prompt
            return ToolAskAnswer('{"answer":"继续按你当前的要求来","reply_kind":"general","referenced_items":[]}', 4)

        monkeypatch.setattr(model, "ask_with_tools", after_withdrawal)
        third = await client.post("/v1/ask", json={
            "question": "继续", "conversation_id": str(conversation_id),
            "expected_revision": second.json()["design_brief"]["revision"], "request_id": str(uuid.uuid4()),
        })
        assert third.status_code == 200, third.text
    state = await db_session.get(AskConversationState, conversation_id)
    assert state is not None and marker not in (state.summary or "")
    assert marker not in (state.active_constraints or [])
