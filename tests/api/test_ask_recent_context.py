import json
import uuid
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports.llm import ToolAskAnswer, ToolExecutor, ToolTrace, ToolUse
from app.application.preferences.memory import forget_preference
from app.config.settings import Settings
from app.infrastructure.persistence.models.ask_conversation import AskConversationState
from app.infrastructure.persistence.models.ask_session import AskSession
from app.infrastructure.persistence.models.style import UserStyleProfile
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.repositories.ask_conversation_repo import SqlAlchemyAskConversationRepository
from app.infrastructure.persistence.repositories.preference_memory_repo import SqlAlchemyPreferenceMemoryRepository
from app.presentation.api.v1.routers import ask
from tests.api.test_ask import _build_app, _FakeLlm, _seed_user_and_items
from tests.api.test_designer_sessions import _setup


def _prompt_context(model: _FakeLlm) -> dict:
    assert model.last_system_prompt is not None
    payload = next(
        json.loads(line) for line in model.last_system_prompt.splitlines() if line.startswith('{"context":')
    )
    return payload["context"].get("conversation_state", {})


async def test_followup_uses_latest_messages_and_rolls_the_summary_forward(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = await _seed_user_and_items(db_session)
    settings = Settings(APP_ENV="dev", ENABLE_ASK_AIDROBE=True, ENABLE_ASK_CONVERSATIONS=True)
    for module in (ask, ask.ask_uc):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    model = _FakeLlm(text='{"answer":"New answer","referenced_items":[]}')
    app = _build_app(db_session, settings, llm=model)
    repo = SqlAlchemyAskConversationRepository(db_session)
    conversation = await repo.create_conversation(user_id=user_id)
    other = await repo.create_conversation(user_id=user_id)
    start = datetime.now(UTC) - timedelta(hours=1)
    for index in range(30):
        for role, label in (("user", "q"), ("assistant", "a")):
            await repo.append_message(
                conversation=conversation, role=role, content=f"{label}-{index:02d}",
                created_at=start + timedelta(minutes=index),
            )
    await repo.append_message(conversation=other, role="user", content="Other conversation secret")
    db_session.add(AskConversationState(
        conversation_id=conversation.id, user_id=user_id, summary="Stale oldest summary",
    ))
    await db_session.flush()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver",
                           headers={"X-User-Id": str(user_id)}) as client:
        response = await client.post("/v1/ask", json={
            "question": "Continue the last change", "conversation_id": str(conversation.id),
        })
        assert response.status_code == 200, response.text
        recent = _prompt_context(model)["recent_messages"]
        assert [(entry["role"], entry["content"]) for entry in recent] == [
            (role, f"{label}-{index:02d}")
            for index in range(27, 30) for role, label in (("user", "q"), ("assistant", "a"))
        ]
        assert "Other conversation secret" not in model.last_system_prompt
        state = await repo.get_state(user_id, conversation.id)
        assert state is not None and state.summary is not None
        assert "q-27" in state.summary and "q-28" not in state.summary
        assert "q-00" not in state.summary and "Stale oldest summary" not in state.summary

        followup = await client.post("/v1/ask", json={
            "question": "Keep the same trousers", "conversation_id": str(conversation.id),
        })
        assert followup.status_code == 200, followup.text
        assert state.summary is not None and "q-28" in state.summary
        assert state.summary.count("q-27") == 1
        assert "historical conversation data" in model.last_system_prompt

    outsider = User()
    db_session.add(outsider)
    await db_session.flush()
    assert await repo.list_messages(outsider.id, conversation.id, limit=6, latest=True) == []


async def test_forgetting_a_preference_excludes_pre_reset_original_messages(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = await _seed_user_and_items(db_session)
    settings = Settings(APP_ENV="dev", ENABLE_ASK_AIDROBE=True, ENABLE_ASK_CONVERSATIONS=True)
    for module in (ask, ask.ask_uc):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    model = _FakeLlm(text='{"answer":"Continue","referenced_items":[]}')
    app = _build_app(db_session, settings, llm=model)
    repo = SqlAlchemyAskConversationRepository(db_session)
    conversation = await repo.create_conversation(user_id=user_id)
    await repo.append_message(
        conversation=conversation, role="user", content="Revoked original preference: always avoid red",
        created_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    await forget_preference(
        SqlAlchemyPreferenceMemoryRepository(db_session), user_id=user_id,
        preference_type="color", preference_value="red",
    )
    user = await db_session.get(User, user_id)
    assert user is not None and user.memory_context_reset_at is not None
    await repo.append_message(
        conversation=conversation, role="assistant", content="Exactly at the reset cutoff",
        created_at=user.memory_context_reset_at,
    )
    await repo.append_message(
        conversation=conversation, role="user", content="Keep the blue trousers for this outfit",
        created_at=user.memory_context_reset_at + timedelta(microseconds=1),
    )
    await db_session.flush()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver",
                           headers={"X-User-Id": str(user_id)}) as client:
        response = await client.post("/v1/ask", json={
            "question": "Continue", "conversation_id": str(conversation.id),
        })
    assert response.status_code == 200, response.text
    recent = _prompt_context(model)["recent_messages"]
    assert recent == [{"role": "user", "content": "Keep the blue trousers for this outfit"}]
    assert "Revoked original preference" not in model.last_system_prompt
    assert "Exactly at the reset cutoff" not in model.last_system_prompt


@pytest.mark.parametrize("source_state", ["available", "deleted", "foreign", "failed"])
async def test_continued_design_reuses_only_available_owned_photo_observations(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, source_state: str,
) -> None:
    app, user_id, _, model, _ = await _setup(db_session, monkeypatch)
    source = UserStyleProfile(
        user_id=user_id, source_type="inspiration_image", learn_status="learned",
        image_url="https://cdn.example.com/private-photo.jpg",
        learned_summary={"source": "vision", "style_tags": ["minimal"], "color_palette": ["red", "black"]},
    )
    db_session.add(source)
    await db_session.commit()
    source_id = source.id
    analysis = AsyncMock(side_effect=AssertionError("A follow-up must not rerun photo analysis"))
    monkeypatch.setattr(ask.ask_uc, "learn_from_image", analysis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver",
                           headers={"X-User-Id": str(user_id)}) as client:
        first = await client.post("/v1/ask", json={
            "question": "Use this reference", "inspiration_id": str(source_id),
            "inspiration_only": True, "request_id": str(uuid.uuid4()),
        })
        assert first.status_code == 200, first.text
        draft = first.json()
        if source_state == "deleted":
            await db_session.delete(source)
        elif source_state == "foreign":
            other = User()
            db_session.add(other)
            await db_session.flush()
            source.user_id = other.id
        elif source_state == "failed":
            source.learn_status = "failed"
        await db_session.commit()
        model.clarify = True
        provider = AsyncMock(wraps=model.ask_with_tools)
        monkeypatch.setattr(model, "ask_with_tools", provider)
        response = await client.post("/v1/ask", json={
            "question": "Keep the trousers and reconsider the shoes",
            "conversation_id": draft["conversation_id"],
            "expected_revision": draft["design_brief"]["revision"], "request_id": str(uuid.uuid4()),
        })
    assert response.status_code == 200, response.text
    prompt = provider.call_args.args[0]
    if source_state == "available":
        assert 'Reference: {"styles": ["minimal"], "palette": ["black", "red"]}' in prompt
    else:
        assert "Reference: " not in prompt
        assert "saved photo observations are currently unavailable" in prompt
    assert str(source_id) not in prompt and "private-photo.jpg" not in prompt
    assert response.json()["design_brief"]["constraints"]["inspiration_id"] == str(source_id)
    outfit_id = draft["styling"]["outfit"]["outfit_id"]
    assert response.json()["styling"]["outfit"]["outfit_id"] == outfit_id
    analysis.assert_not_awaited()


@pytest.mark.parametrize("failure_reason", [None, "deadline_exceeded"])
@pytest.mark.parametrize("update_brief", [False, True])
async def test_failed_tool_trace_survives_fallback_and_idempotent_replay(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, failure_reason: str | None, update_brief: bool,
) -> None:
    app, user_id, _, model, _ = await _setup(db_session, monkeypatch)
    trace = ToolTrace(
        tool="validate_outfit", arguments={"positions": [1, 2]},
        result={"error": "missing shoes"}, status="error", duration_ms=12,
    )

    async def interrupted(
        _prompt: str, question: str, _tools: object, execute: ToolExecutor, **_kwargs: object,
    ) -> ToolAskAnswer:
        if update_brief:
            updated = await execute("update_design_brief", {
                "evidence_text": question, "target_style": "vivid, casual", "scene": "casual",
            })
            assert updated.content["updated"] is True
        return ToolAskAnswer(
            text="", tokens_used=13, used=[ToolUse("get_weather", "Test weather: cloudy 16°C")],
            tool_trace=[trace], failure_reason=failure_reason,
        )

    provider = AsyncMock(side_effect=interrupted)
    extra_call = AsyncMock(side_effect=AssertionError("A traced failure must not call the provider again"))
    monkeypatch.setattr(model, "ask", extra_call)
    monkeypatch.setattr(model, "ask_with_tools", provider)
    request = {"question": "Design a brighter casual outfit", "design_mode": True, "request_id": str(uuid.uuid4())}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver",
                           headers={"X-User-Id": str(user_id)}) as client:
        response = await client.post("/v1/ask", json=request)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["answer"].startswith("Test weather: cloudy 16°C")
        assert body["tokens_used"] == 13
        assert body["failure_reason"] == (failure_reason or "empty_response")
        if update_brief:
            assert "刚才的设计条件已保留" in body["answer"]
            assert body["design_brief"]["constraints"]["target_style"] == "vivid, casual"
        else:
            assert "没能核实穿搭建议" in body["answer"]
        assert body["tool_trace"] == [{
            "tool": "validate_outfit", "arguments": {"positions": [1, 2]},
            "result": {"error": "missing shoes"}, "status": "error", "duration_ms": 12,
        }]
        saved = await db_session.get(AskSession, uuid.UUID(body["ask_session_id"]))
        assert saved is not None and saved.context["tool_trace"] == body["tool_trace"]
        replay = await client.post("/v1/ask", json=request)
        assert replay.status_code == 200, replay.text
        assert replay.json()["tool_trace"] == body["tool_trace"]
        assert replay.json()["used"] == body["used"]
        assert replay.json()["failure_reason"] == body["failure_reason"]
    provider.assert_awaited_once()
    extra_call.assert_not_awaited()


@pytest.mark.parametrize("failure_reason", ["deadline_exceeded", "provider_timeout"])
async def test_first_provider_timeout_does_not_start_another_paid_request(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, failure_reason: str,
) -> None:
    app, user_id, _, model, _ = await _setup(db_session, monkeypatch)
    provider = AsyncMock(return_value=ToolAskAnswer(text="", tokens_used=0, failure_reason=failure_reason))
    extra_call = AsyncMock(side_effect=AssertionError("The tool deadline must also cover an empty first request"))
    monkeypatch.setattr(model, "ask_with_tools", provider)
    monkeypatch.setattr(model, "ask", extra_call)
    request = {"question": "Choose a casual outfit", "design_mode": True, "request_id": str(uuid.uuid4())}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver",
                           headers={"X-User-Id": str(user_id)}) as client:
        response = await client.post("/v1/ask", json=request)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["failure_reason"] == failure_reason and body["tokens_used"] == 0
        assert body["tool_trace"] == [] and body["used"] == [] and body["item_refs"] == []
        assert "没能核实穿搭建议" in body["answer"]
        replay = await client.post("/v1/ask", json=request)
        assert replay.status_code == 200 and replay.json()["failure_reason"] == failure_reason
    provider.assert_awaited_once()
    extra_call.assert_not_awaited()


@pytest.mark.parametrize(("output", "failure_reason"), [
    ("An invalid structured answer", "invalid_output"),
    ('{"answer":"Wear this outfit","reply_kind":"outfit","referenced_items":[1,2]}', "unverified_outfit"),
])
@pytest.mark.parametrize("update_brief", [False, True])
async def test_final_answer_failure_stage_is_saved_for_replay(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, output: str, failure_reason: str, update_brief: bool,
) -> None:
    app, user_id, _, model, _ = await _setup(db_session, monkeypatch)

    async def invalid_answer(
        _prompt: str, question: str, _tools: object, execute: ToolExecutor, **_kwargs: object,
    ) -> ToolAskAnswer:
        if update_brief:
            updated = await execute("update_design_brief", {"evidence_text": question, "goal": question})
            assert updated.content["updated"] is True
        return ToolAskAnswer(text=output, tokens_used=8)

    monkeypatch.setattr(model, "ask_with_tools", AsyncMock(side_effect=invalid_answer))
    request = {"question": "Choose an outfit", "design_mode": True, "request_id": str(uuid.uuid4())}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver",
                           headers={"X-User-Id": str(user_id)}) as client:
        response = await client.post("/v1/ask", json=request)
        assert response.status_code == 200, response.text
        assert response.json()["failure_reason"] == failure_reason
        assert response.json()["item_refs"] == []
        if update_brief:
            assert "刚才的设计条件已保留" in response.json()["answer"]
            assert response.json()["design_brief"]["constraints"]["goal"] == request["question"]
        replay = await client.post("/v1/ask", json=request)
        assert replay.status_code == 200, replay.text
        assert replay.json()["failure_reason"] == failure_reason
