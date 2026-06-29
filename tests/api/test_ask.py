"""M15 Ask Aidrobe — flag gate, happy path (fake LLM), history, quota 429."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports.llm import LlmAnswer
from app.application.preferences.memory import read_preference_memory
from app.bootstrap.container import llm_service
from app.common.enums import Category, ImageType, ItemStatus
from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.ask_conversation import AskConversation
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem, WardrobeItemImage
from app.infrastructure.persistence.repositories.ask_conversation_repo import (
    SqlAlchemyAskConversationRepository,
)
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import ask as ask_router
from app.presentation.middlewares.exception_handler import setup_exception_handlers


class _FakeLlm:
    """Deterministic stand-in for LlmPort.ask — no HTTP, fixed tokens."""

    def __init__(self, *, text: str = "建议搭配白衬衫和黑裤子。", tokens: int = 42) -> None:
        self._text = text
        self._tokens = tokens
        self.last_system_prompt: str | None = None
        self.last_json_mode: bool | None = None

    async def polish_reason(self, prompt: str) -> str:
        return ""

    async def ask(
        self, system_prompt: str, question: str, *, json_mode: bool = False
    ) -> LlmAnswer:
        self.last_system_prompt = system_prompt
        self.last_json_mode = json_mode
        return LlmAnswer(text=self._text, tokens_used=self._tokens)


def _build_app(
    session: AsyncSession,
    settings_override: Settings,
    *,
    llm: object | None = None,
) -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)

    async def override_db_session() -> AsyncSession:
        return session

    def override_settings() -> Settings:
        return settings_override

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = override_settings
    if llm is not None:
        app.dependency_overrides[llm_service] = lambda: llm
    app.include_router(ask_router.router)
    return app


@pytest.fixture
def patch_router_settings(monkeypatch: pytest.MonkeyPatch):
    """The router's `_gate()` calls `get_settings()` directly (lru-cached,
    bypasses dep override). Flag-on tests must patch the module-level
    `get_settings` — matches the cost_guard / today_context pattern."""

    def _factory(**overrides) -> Settings:  # type: ignore[no-untyped-def]
        merged = Settings(APP_ENV="dev", **overrides)
        monkeypatch.setattr(
            "app.presentation.api.v1.routers.ask.get_settings", lambda: merged
        )
        return merged

    return _factory


async def _seed_user_and_items(session: AsyncSession) -> uuid.UUID:
    user_id = uuid.uuid4()
    session.add(User(id=user_id))
    await session.flush()
    for title, category, color in [
        ("White shirt", Category.TOP.value, "white"),
        ("Black pants", Category.BOTTOM.value, "black"),
    ]:
        session.add(
            WardrobeItem(
                user_id=user_id,
                title=title,
                category=category,
                color_main=color,
                status=ItemStatus.WEARABLE.value,
            )
        )
    await session.flush()
    return user_id


async def test_ask_returns_503_when_flag_off(db_session: AsyncSession) -> None:
    user_id = await _seed_user_and_items(db_session)
    app = _build_app(
        db_session,
        Settings(APP_ENV="dev", ENABLE_ASK_AIDROBE=False),
        llm=_FakeLlm(),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/ask",
            headers={"X-User-Id": str(user_id)},
            json={"question": "今天穿什么？"},
        )
    assert resp.status_code == 503


async def test_ask_structured_disabled_returns_no_refs(
    db_session: AsyncSession,
    patch_router_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BE#28 — structured OFF with no focused item must NOT echo back every
    candidate as item_refs; without a structured signal we can't tell which
    items the model cited, so item_refs degrades to empty."""
    user_id = await _seed_user_and_items(db_session)
    patch_router_settings(ENABLE_ASK_AIDROBE=True)
    legacy = Settings(
        APP_ENV="dev", ENABLE_ASK_AIDROBE=True, ENABLE_ASK_STRUCTURED_OUTPUT=False
    )
    monkeypatch.setattr(
        "app.application.ask.answer_question.get_settings", lambda: legacy
    )
    app = _build_app(
        db_session,
        Settings(APP_ENV="dev", ENABLE_ASK_AIDROBE=True),
        llm=_FakeLlm(),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/ask",
            headers={"X-User-Id": str(user_id)},
            json={"question": "今天有客户会，穿哪套更稳？", "context": {"scene": "business"}},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "建议搭配白衬衫和黑裤子。"
    assert body["tokens_used"] == 42
    # No structured signal + no focus -> no refs (NOT all candidates).
    assert body["item_refs"] == []
    assert body["has_wardrobe_context"] is True
    assert body["locale"] == "zh-CN"
    assert uuid.UUID(body["ask_session_id"])


async def test_ask_structured_disabled_with_focus_returns_focused_only(
    db_session: AsyncSession,
    patch_router_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BE#28 — structured OFF but a focused item set: item_refs is exactly that
    one focused item, never the whole candidate set."""
    user_id = await _seed_user_and_items(db_session)
    focus_id = await _seed_focused_item(db_session, user_id)
    patch_router_settings(ENABLE_ASK_AIDROBE=True)
    legacy = Settings(
        APP_ENV="dev", ENABLE_ASK_AIDROBE=True, ENABLE_ASK_STRUCTURED_OUTPUT=False
    )
    monkeypatch.setattr(
        "app.application.ask.answer_question.get_settings", lambda: legacy
    )
    app = _build_app(
        db_session,
        Settings(APP_ENV="dev", ENABLE_ASK_AIDROBE=True),
        llm=_FakeLlm(),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/ask",
            headers={"X-User-Id": str(user_id)},
            json={"question": "用这件搭一套", "focused_item_id": str(focus_id)},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["item_refs"] == [str(focus_id)]
    assert body["has_wardrobe_context"] is True


async def test_ask_feeds_wardrobe_into_prompt(
    db_session: AsyncSession, patch_router_settings
) -> None:
    """Regression: the sanitized wardrobe MUST reach the LLM system prompt.

    A previous double-sanitization bug ran the `{"items": [...]}` container
    through `sanitize_for_llm`, whose whitelist has no "items" key, silently
    stripping the entire wardrobe so the LLM answered with zero context."""
    user_id = await _seed_user_and_items(db_session)
    patch_router_settings(ENABLE_ASK_AIDROBE=True)
    llm = _FakeLlm()
    app = _build_app(
        db_session,
        Settings(APP_ENV="dev", ENABLE_ASK_AIDROBE=True),
        llm=llm,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/ask",
            headers={"X-User-Id": str(user_id)},
            json={"question": "今天穿什么？"},
        )
    assert resp.status_code == 200
    assert llm.last_system_prompt is not None
    # Both seeded items' structured attributes are present in the prompt.
    assert '"category": "top"' in llm.last_system_prompt
    assert '"color_main": "white"' in llm.last_system_prompt
    assert '"category": "bottom"' in llm.last_system_prompt
    assert '"color_main": "black"' in llm.last_system_prompt
    # The opaque user token is still attached for cross-turn identity.
    assert "usr_" in llm.last_system_prompt


async def test_ask_without_items_reports_no_wardrobe_context(
    db_session: AsyncSession, patch_router_settings
) -> None:
    user_id = uuid.uuid4()
    db_session.add(User(id=user_id))
    await db_session.flush()
    patch_router_settings(ENABLE_ASK_AIDROBE=True)
    llm = _FakeLlm()
    app = _build_app(
        db_session,
        Settings(APP_ENV="dev", ENABLE_ASK_AIDROBE=True),
        llm=llm,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/ask",
            headers={"X-User-Id": str(user_id)},
            json={"question": "今天穿什么？"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["item_refs"] == []
    assert body["item_ref_snapshots"] == []
    assert body["has_wardrobe_context"] is False
    assert body["locale"] == "zh-CN"
    assert llm.last_system_prompt is not None
    assert "本次没有可用的衣橱单品上下文" in llm.last_system_prompt


async def _seed_focused_item(session: AsyncSession, user_id: uuid.UUID) -> uuid.UUID:
    """Add one distinctive item to an existing user and return its id."""
    item = WardrobeItem(
        user_id=user_id,
        title="Beige trench",
        category=Category.OUTERWEAR.value,
        color_main="beige",
        status=ItemStatus.WEARABLE.value,
    )
    session.add(item)
    await session.flush()
    return item.id


async def test_ask_focused_item_pins_item_and_adds_focus_clause(
    db_session: AsyncSession, patch_router_settings
) -> None:
    user_id = await _seed_user_and_items(db_session)
    focus_id = await _seed_focused_item(db_session, user_id)
    patch_router_settings(ENABLE_ASK_AIDROBE=True)
    llm = _FakeLlm()
    app = _build_app(
        db_session,
        Settings(APP_ENV="dev", ENABLE_ASK_AIDROBE=True),
        llm=llm,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/ask",
            headers={"X-User-Id": str(user_id)},
            json={"question": "用这件搭一套", "focused_item_id": str(focus_id)},
        )
    assert resp.status_code == 200
    assert llm.last_system_prompt is not None
    # Focus clause present and the focused item's attribute reaches the prompt.
    assert "第 1 件" in llm.last_system_prompt
    assert '"color_main": "beige"' in llm.last_system_prompt


async def test_ask_focused_item_404_when_not_owned(
    db_session: AsyncSession, patch_router_settings
) -> None:
    user_id = await _seed_user_and_items(db_session)
    patch_router_settings(ENABLE_ASK_AIDROBE=True)
    app = _build_app(
        db_session,
        Settings(APP_ENV="dev", ENABLE_ASK_AIDROBE=True),
        llm=_FakeLlm(),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/ask",
            headers={"X-User-Id": str(user_id)},
            json={"question": "用这件搭一套", "focused_item_id": str(uuid.uuid4())},
        )
    assert resp.status_code == 404


async def test_ask_locale_switches_preamble_language(
    db_session: AsyncSession, patch_router_settings
) -> None:
    """A request `locale` flows through to the system-prompt preamble."""
    user_id = await _seed_user_and_items(db_session)
    patch_router_settings(ENABLE_ASK_AIDROBE=True)
    llm = _FakeLlm()
    app = _build_app(
        db_session,
        Settings(APP_ENV="dev", ENABLE_ASK_AIDROBE=True),
        llm=llm,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/ask",
            headers={"X-User-Id": str(user_id)},
            json={"question": "what should I wear?", "locale": "en-US"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["locale"] == "en-US"
    assert body["has_wardrobe_context"] is True
    assert llm.last_system_prompt is not None
    assert llm.last_system_prompt.startswith("You are Aidrobe")
    # Wardrobe still reaches the prompt regardless of locale.
    assert '"category": "top"' in llm.last_system_prompt


async def test_ask_structured_validates_item_refs(
    db_session: AsyncSession,
    patch_router_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With structured output on, item_refs is the validated cited subset and
    the human answer is unwrapped from the JSON envelope."""
    user_id = await _seed_user_and_items(db_session)  # 2 wearable items
    patch_router_settings(ENABLE_ASK_AIDROBE=True)
    structured = Settings(
        APP_ENV="dev", ENABLE_ASK_AIDROBE=True, ENABLE_ASK_STRUCTURED_OUTPUT=True
    )
    monkeypatch.setattr(
        "app.application.ask.answer_question.get_settings", lambda: structured
    )
    # Model cites position 1 (valid) and 9 (hallucinated, out of range).
    llm = _FakeLlm(
        text='{"answer": "穿白衬衫吧", "referenced_items": [1, 9]}'
    )
    app = _build_app(
        db_session,
        Settings(APP_ENV="dev", ENABLE_ASK_AIDROBE=True),
        llm=llm,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/ask",
            headers={"X-User-Id": str(user_id)},
            json={"question": "今天穿什么？"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "穿白衬衫吧"  # unwrapped from the JSON envelope
    assert len(body["item_refs"]) == 1  # only the in-range position survives
    assert body["has_wardrobe_context"] is True
    assert llm.last_json_mode is True  # provider constrained to JSON mode


async def test_ask_structured_persists_explicit_stated_preferences(
    db_session: AsyncSession,
    patch_router_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BE#46 — the trusted write path: a preference the user explicitly stated is
    persisted from the structured envelope. Persistence is unconditional (the
    read flag stays OFF here), and a bad type is dropped fail-soft, never failing
    the answer."""
    from app.infrastructure.persistence.repositories.preference_memory_repo import (
        SqlAlchemyPreferenceMemoryRepository,
    )

    user_id = await _seed_user_and_items(db_session)
    patch_router_settings(ENABLE_ASK_AIDROBE=True)
    # Note: ENABLE_PREFERENCE_MEMORY is OFF — writes still happen.
    structured = Settings(
        APP_ENV="dev", ENABLE_ASK_AIDROBE=True, ENABLE_ASK_STRUCTURED_OUTPUT=True
    )
    monkeypatch.setattr(
        "app.application.ask.answer_question.get_settings", lambda: structured
    )
    llm = _FakeLlm(
        text=(
            '{"answer": "ok", "referenced_items": [], "stated_preferences": ['
            '{"type": "fit", "value": "oversized shirts", "polarity": "negative"},'
            '{"type": "bogus", "value": "junk", "polarity": "negative"}]}'
        )
    )
    app = _build_app(
        db_session,
        Settings(APP_ENV="dev", ENABLE_ASK_AIDROBE=True),
        llm=llm,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/ask",
            headers={"X-User-Id": str(user_id)},
            json={"question": "I dislike oversized shirts. What should I wear?"},
        )
    assert resp.status_code == 200

    repo = SqlAlchemyPreferenceMemoryRepository(db_session)
    rows = await repo.list_live(user_id)
    # The valid explicit preference is persisted; the bad-type one is dropped.
    assert len(rows) == 1
    row = rows[0]
    assert row.preference_type == "fit"
    assert row.preference_value == "oversized shirts"
    assert row.polarity == "negative"
    assert row.source == "explicit_user_statement"
    assert row.source_message_id is not None
    # Read view confirms the shape too.
    memory = await read_preference_memory(repo, user_id)
    assert {e["value"] for e in memory["negative"]} == {"oversized shirts"}


async def test_ask_conversation_initial_creation(
    db_session: AsyncSession,
    patch_router_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = await _seed_user_and_items(db_session)
    patch_router_settings(ENABLE_ASK_AIDROBE=True, ENABLE_ASK_CONVERSATIONS=True)
    structured = Settings(
        APP_ENV="dev",
        ENABLE_ASK_AIDROBE=True,
        ENABLE_ASK_STRUCTURED_OUTPUT=True,
        ENABLE_ASK_CONVERSATIONS=True,
    )
    monkeypatch.setattr("app.application.ask.answer_question.get_settings", lambda: structured)
    llm = _FakeLlm(
        text=(
            '{"answer": "ok", "referenced_items": [], '
            '"conversation_deltas": {"reasoning": "first outfit", '
            '"applied_constraints": ["business casual"]}}'
        )
    )
    app = _build_app(db_session, Settings(APP_ENV="dev", ENABLE_ASK_AIDROBE=True), llm=llm)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/ask",
            headers={"X-User-Id": str(user_id)},
            json={"question": "Give me a business casual outfit"},
        )
    assert resp.status_code == 200
    body = resp.json()
    conversation_id = uuid.UUID(body["conversation_id"])
    assert body["deltas"] == {
        "reasoning": "first outfit",
        "applied_constraints": ["business casual"],
    }
    repo = SqlAlchemyAskConversationRepository(db_session)
    state = await repo.get_state(user_id, conversation_id)
    assert state is not None
    assert state.active_constraints == ["business casual"]
    conversation = await repo.get_owned_conversation(user_id, conversation_id)
    assert conversation is not None
    assert conversation.message_count == 2  # one user + one assistant message


async def test_ask_conversation_follow_up_uses_same_conversation(
    db_session: AsyncSession,
    patch_router_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = await _seed_user_and_items(db_session)
    patch_router_settings(ENABLE_ASK_AIDROBE=True, ENABLE_ASK_CONVERSATIONS=True)
    structured = Settings(
        APP_ENV="dev",
        ENABLE_ASK_AIDROBE=True,
        ENABLE_ASK_STRUCTURED_OUTPUT=True,
        ENABLE_ASK_CONVERSATIONS=True,
    )
    monkeypatch.setattr("app.application.ask.answer_question.get_settings", lambda: structured)
    llm = _FakeLlm(
        text=(
            '{"answer": "ok", "referenced_items": [], '
            '"conversation_deltas": {"reasoning": "refined", '
            '"applied_constraints": ["more casual"]}}'
        )
    )
    app = _build_app(db_session, Settings(APP_ENV="dev", ENABLE_ASK_AIDROBE=True), llm=llm)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        first = await client.post(
            "/v1/ask",
            headers={"X-User-Id": str(user_id)},
            json={"question": "Start an outfit"},
        )
        conversation_id = first.json()["conversation_id"]
        second = await client.post(
            "/v1/ask",
            headers={"X-User-Id": str(user_id)},
            json={"question": "Make it more casual", "conversation_id": conversation_id},
        )
    assert second.status_code == 200
    assert second.json()["conversation_id"] == conversation_id
    assert llm.last_system_prompt is not None
    assert '"conversation_state"' in llm.last_system_prompt
    repo = SqlAlchemyAskConversationRepository(db_session)
    state = await repo.get_state(user_id, uuid.UUID(conversation_id))
    assert state is not None
    assert "more casual" in state.active_constraints
    conversation = await repo.get_owned_conversation(user_id, uuid.UUID(conversation_id))
    assert conversation is not None
    assert conversation.message_count == 4  # two turns x (user + assistant)


async def test_ask_conversation_focused_item_rejection(
    db_session: AsyncSession,
    patch_router_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = await _seed_user_and_items(db_session)
    focus_id = await _seed_focused_item(db_session, user_id)
    patch_router_settings(ENABLE_ASK_AIDROBE=True, ENABLE_ASK_CONVERSATIONS=True)
    structured = Settings(
        APP_ENV="dev",
        ENABLE_ASK_AIDROBE=True,
        ENABLE_ASK_STRUCTURED_OUTPUT=True,
        ENABLE_ASK_CONVERSATIONS=True,
    )
    monkeypatch.setattr("app.application.ask.answer_question.get_settings", lambda: structured)
    # Rejection now comes from the STRUCTURED signal, not substring sniffing.
    llm = _FakeLlm(
        text=(
            '{"answer": "ok", "referenced_items": [], '
            '"conversation_deltas": {"rejected_focused_item": true}}'
        )
    )
    app = _build_app(db_session, Settings(APP_ENV="dev", ENABLE_ASK_AIDROBE=True), llm=llm)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/ask",
            headers={"X-User-Id": str(user_id)},
            json={"question": "不要这件，换一件", "focused_item_id": str(focus_id)},
        )
    assert resp.status_code == 200
    assert "exclude focused item" in resp.json()["deltas"]["applied_constraints"]
    assert llm.last_system_prompt is not None
    # The focused item is now SHOWN this turn (it's the same prompt that carried
    # the rejection signal back); durable exclusion takes effect next turn.
    assert '"color_main": "beige"' in llm.last_system_prompt
    state = await SqlAlchemyAskConversationRepository(db_session).get_state(
        user_id, uuid.UUID(resp.json()["conversation_id"])
    )
    assert state is not None
    assert state.rejected_item_ids == [str(focus_id)]


async def test_ask_conversation_state_retrieval(
    db_session: AsyncSession,
    patch_router_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = await _seed_user_and_items(db_session)
    patch_router_settings(ENABLE_ASK_AIDROBE=True, ENABLE_ASK_CONVERSATIONS=True)
    structured = Settings(
        APP_ENV="dev",
        ENABLE_ASK_AIDROBE=True,
        ENABLE_ASK_STRUCTURED_OUTPUT=True,
        ENABLE_ASK_CONVERSATIONS=True,
    )
    monkeypatch.setattr("app.application.ask.answer_question.get_settings", lambda: structured)
    app = _build_app(
        db_session,
        Settings(APP_ENV="dev", ENABLE_ASK_AIDROBE=True),
        llm=_FakeLlm(
            text=(
                '{"answer": "ok", "referenced_items": [], '
                '"conversation_deltas": {"applied_constraints": ["rain"]}}'
            )
        ),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/ask",
            headers={"X-User-Id": str(user_id)},
            json={"question": "What if it rains?", "context": {"scene": "commute"}},
        )
    conversation_id = uuid.UUID(resp.json()["conversation_id"])
    repo = SqlAlchemyAskConversationRepository(db_session)
    conversation = await repo.get_owned_conversation(user_id, conversation_id)
    state = await repo.get_state(user_id, conversation_id)
    assert conversation is not None
    assert state is not None
    assert state.conversation_id == conversation.id
    assert state.active_constraints == ["rain", "scene:commute"]


async def test_ask_conversation_flag_off_hides_response_fields_but_persists(
    db_session: AsyncSession,
    patch_router_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = await _seed_user_and_items(db_session)
    patch_router_settings(ENABLE_ASK_AIDROBE=True, ENABLE_ASK_CONVERSATIONS=False)
    structured = Settings(APP_ENV="dev", ENABLE_ASK_AIDROBE=True, ENABLE_ASK_STRUCTURED_OUTPUT=True)
    monkeypatch.setattr("app.application.ask.answer_question.get_settings", lambda: structured)
    app = _build_app(
        db_session,
        Settings(APP_ENV="dev", ENABLE_ASK_AIDROBE=True),
        llm=_FakeLlm(
            text=(
                '{"answer": "ok", "referenced_items": [], '
                '"conversation_deltas": {"applied_constraints": ["hidden"]}}'
            )
        ),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/ask",
            headers={"X-User-Id": str(user_id)},
            json={"question": "Start hidden conversation"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["conversation_id"] is None
    assert body["deltas"] is None
    persisted = await db_session.scalar(select(AskConversation).where(AskConversation.user_id == user_id))
    assert persisted is not None
    state = await SqlAlchemyAskConversationRepository(db_session).get_state(user_id, persisted.id)
    assert state is not None
    assert state.active_constraints == ["hidden"]


async def test_ask_conversation_flag_off_is_noop_on_answer_context(
    db_session: AsyncSession,
    patch_router_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """FIX 1 — with ENABLE_ASK_CONVERSATIONS OFF, a supplied (valid)
    conversation_id must NOT alter the answer context: prior conversation_state
    is never injected into the prompt, and the durable-rejection pool exclusion
    is not applied. Net effect == no conversation_id."""
    user_id = await _seed_user_and_items(db_session)
    # The white top is one of the seeded wearable candidates; pre-mark it rejected
    # in prior conversation state so we can prove the pool gate is NOT applied.
    rejected_item = await db_session.scalar(
        select(WardrobeItem).where(
            WardrobeItem.user_id == user_id, WardrobeItem.color_main == "white"
        )
    )
    assert rejected_item is not None
    repo = SqlAlchemyAskConversationRepository(db_session)
    conversation = await repo.create_conversation(user_id=user_id, title="prior")
    await repo.upsert_state(
        conversation=conversation,
        state=None,
        last_outfit=None,
        active_constraints=["formal only"],
        rejected_item_ids=[str(rejected_item.id)],
        preferred_item_ids=[],
        summary="earlier we discussed formalwear",
    )
    # Router exposure flag ON (so we get a 200), but conversations OFF on the
    # consuming module — that's the gate under test.
    patch_router_settings(ENABLE_ASK_AIDROBE=True, ENABLE_ASK_CONVERSATIONS=False)
    off = Settings(
        APP_ENV="dev",
        ENABLE_ASK_AIDROBE=True,
        ENABLE_ASK_STRUCTURED_OUTPUT=True,
        ENABLE_ASK_CONVERSATIONS=False,
    )
    monkeypatch.setattr("app.application.ask.answer_question.get_settings", lambda: off)
    llm = _FakeLlm(text='{"answer": "ok", "referenced_items": []}')
    app = _build_app(db_session, Settings(APP_ENV="dev", ENABLE_ASK_AIDROBE=True), llm=llm)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/ask",
            headers={"X-User-Id": str(user_id)},
            json={"question": "今天穿什么？", "conversation_id": str(conversation.id)},
        )
    assert resp.status_code == 200
    assert llm.last_system_prompt is not None
    # Prior conversation state is NOT injected into the prompt.
    assert '"conversation_state"' not in llm.last_system_prompt
    assert "formal only" not in llm.last_system_prompt
    assert "earlier we discussed formalwear" not in llm.last_system_prompt
    # The durably-rejected item is NOT excluded from the candidate pool when the
    # flag is off, so its attribute still reaches the prompt.
    assert '"color_main": "white"' in llm.last_system_prompt


async def test_ask_structured_parse_failure_keeps_text_drops_refs(
    db_session: AsyncSession,
    patch_router_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If the model ignores the JSON contract, keep its text as the answer but
    emit no item_refs (stay honest rather than surface uncited items)."""
    user_id = await _seed_user_and_items(db_session)
    patch_router_settings(ENABLE_ASK_AIDROBE=True)
    structured = Settings(
        APP_ENV="dev", ENABLE_ASK_AIDROBE=True, ENABLE_ASK_STRUCTURED_OUTPUT=True
    )
    monkeypatch.setattr(
        "app.application.ask.answer_question.get_settings", lambda: structured
    )
    llm = _FakeLlm(text="我觉得你可以穿蓝色。")  # free text, not JSON
    app = _build_app(
        db_session,
        Settings(APP_ENV="dev", ENABLE_ASK_AIDROBE=True),
        llm=llm,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/ask",
            headers={"X-User-Id": str(user_id)},
            json={"question": "今天穿什么？"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "我觉得你可以穿蓝色。"
    assert body["item_refs"] == []


async def test_ask_structured_parse_failure_with_focus_returns_focused_only(
    db_session: AsyncSession,
    patch_router_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BE#28 — parse failure with a focused item falls back to the focused item
    only (never the full candidate set)."""
    user_id = await _seed_user_and_items(db_session)
    focus_id = await _seed_focused_item(db_session, user_id)
    patch_router_settings(ENABLE_ASK_AIDROBE=True)
    structured = Settings(
        APP_ENV="dev", ENABLE_ASK_AIDROBE=True, ENABLE_ASK_STRUCTURED_OUTPUT=True
    )
    monkeypatch.setattr(
        "app.application.ask.answer_question.get_settings", lambda: structured
    )
    llm = _FakeLlm(text="我觉得你可以穿蓝色。")  # free text, not JSON
    app = _build_app(
        db_session,
        Settings(APP_ENV="dev", ENABLE_ASK_AIDROBE=True),
        llm=llm,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/ask",
            headers={"X-User-Id": str(user_id)},
            json={"question": "用这件搭一套", "focused_item_id": str(focus_id)},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["item_refs"] == [str(focus_id)]


async def test_ask_503_when_llm_unavailable(
    db_session: AsyncSession, patch_router_settings
) -> None:
    """LLM_PROVIDER=none (NoOpLlm) returns empty text → 503."""
    user_id = await _seed_user_and_items(db_session)
    patch_router_settings(ENABLE_ASK_AIDROBE=True)
    app = _build_app(
        db_session,
        Settings(APP_ENV="dev", ENABLE_ASK_AIDROBE=True),
        llm=_FakeLlm(text="", tokens=0),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/ask",
            headers={"X-User-Id": str(user_id)},
            json={"question": "今天穿什么？"},
        )
    assert resp.status_code == 503


async def test_ask_history(
    db_session: AsyncSession, patch_router_settings
) -> None:
    user_id = await _seed_user_and_items(db_session)
    patch_router_settings(ENABLE_ASK_AIDROBE=True)
    app = _build_app(
        db_session,
        Settings(APP_ENV="dev", ENABLE_ASK_AIDROBE=True),
        llm=_FakeLlm(),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        await client.post(
            "/v1/ask",
            headers={"X-User-Id": str(user_id)},
            json={"question": "第一个问题？"},
        )
        resp = await client.get("/v1/ask/history", headers={"X-User-Id": str(user_id)})
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["question"] == "第一个问题？"
    assert rows[0]["answer"] == "建议搭配白衬衫和黑裤子。"


async def _seed_item_with_image(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    title: str = "White shirt",
    category: str = Category.TOP.value,
    color: str = "white",
    localized_names: dict[str, str] | None = None,
    object_key: str | None = "wardrobe/abc/standard.jpg",
    deleted: bool = False,
) -> uuid.UUID:
    """Seed one wearable item plus a STANDARD image row (object_key carries the
    thumbnail). Returns the item id."""
    item = WardrobeItem(
        user_id=user_id,
        title=title,
        category=category,
        color_main=color,
        status=ItemStatus.WEARABLE.value,
        localized_names=localized_names,
        deleted_at=datetime.now(UTC) if deleted else None,
        cover_image_url="https://example.com/cover.jpg",
    )
    session.add(item)
    await session.flush()
    session.add(
        WardrobeItemImage(
            item_id=item.id,
            image_type=ImageType.STANDARD.value,
            image_url="https://example.com/standard.jpg",
            object_key=object_key,
            created_at=datetime.now(UTC),
        )
    )
    await session.flush()
    return item.id


def _structured_app(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    llm: object,
) -> FastAPI:
    """Build an Ask app with structured output ON so item_refs is the validated
    cited subset (lets snapshots resolve against real cited items)."""
    structured = Settings(
        APP_ENV="dev", ENABLE_ASK_AIDROBE=True, ENABLE_ASK_STRUCTURED_OUTPUT=True
    )
    monkeypatch.setattr(
        "app.application.ask.answer_question.get_settings", lambda: structured
    )
    return _build_app(
        db_session,
        Settings(APP_ENV="dev", ENABLE_ASK_AIDROBE=True),
        llm=llm,
    )


async def test_ask_global_history_unchanged_without_filters(
    db_session: AsyncSession, patch_router_settings
) -> None:
    """No filters → global history, identical shape to before (now also an
    empty item_ref_snapshots list when there are no refs)."""
    user_id = await _seed_user_and_items(db_session)
    patch_router_settings(ENABLE_ASK_AIDROBE=True)
    app = _build_app(
        db_session, Settings(APP_ENV="dev", ENABLE_ASK_AIDROBE=True), llm=_FakeLlm()
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        await client.post(
            "/v1/ask",
            headers={"X-User-Id": str(user_id)},
            json={"question": "全局问题？"},
        )
        resp = await client.get("/v1/ask/history", headers={"X-User-Id": str(user_id)})
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["question"] == "全局问题？"
    # No refs (structured off, no focus) → empty snapshots, never the candidates.
    assert rows[0]["item_refs"] == []
    assert rows[0]["item_ref_snapshots"] == []


async def test_ask_history_focused_item_filter(
    db_session: AsyncSession, patch_router_settings
) -> None:
    """focused_item_id returns only sessions about that item; the global, non-
    focused session stays out of the scoped view (and old context-less rows by
    construction never match)."""
    user_id = await _seed_user_and_items(db_session)
    focus_id = await _seed_focused_item(db_session, user_id)
    patch_router_settings(ENABLE_ASK_AIDROBE=True)
    app = _build_app(
        db_session, Settings(APP_ENV="dev", ENABLE_ASK_AIDROBE=True), llm=_FakeLlm()
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        # A global (non-focused) session — should be excluded from scoped view.
        await client.post(
            "/v1/ask",
            headers={"X-User-Id": str(user_id)},
            json={"question": "全局问题？"},
        )
        # A focused session about focus_id.
        await client.post(
            "/v1/ask",
            headers={"X-User-Id": str(user_id)},
            json={"question": "用这件搭一套", "focused_item_id": str(focus_id)},
        )
        scoped = await client.get(
            "/v1/ask/history",
            headers={"X-User-Id": str(user_id)},
            params={"focused_item_id": str(focus_id), "scope": "item", "entrypoint": "item_detail"},
        )
        global_view = await client.get(
            "/v1/ask/history", headers={"X-User-Id": str(user_id)}
        )
    assert scoped.status_code == 200
    scoped_rows = scoped.json()
    assert len(scoped_rows) == 1
    assert scoped_rows[0]["question"] == "用这件搭一套"
    # Global view keeps both.
    assert len(global_view.json()) == 2


async def test_ask_item_ref_snapshots_batch_and_localized(
    db_session: AsyncSession,
    patch_router_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Structured output cites two items → both come back as batched snapshots
    with localized names + signed thumbnails."""
    user_id = uuid.uuid4()
    db_session.add(User(id=user_id))
    await db_session.flush()
    id1 = await _seed_item_with_image(
        db_session, user_id, title="White shirt",
        localized_names={"zh-CN": "白色衬衫", "en-US": "White shirt"},
    )
    id2 = await _seed_item_with_image(
        db_session, user_id, title="Black pants", category=Category.BOTTOM.value, color="black",
    )
    patch_router_settings(ENABLE_ASK_AIDROBE=True)
    # Cite both positions (1 and 2).
    llm = _FakeLlm(text='{"answer": "搭这两件", "referenced_items": [1, 2]}')
    app = _structured_app(db_session, monkeypatch, llm)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/ask",
            headers={"X-User-Id": str(user_id)},
            json={"question": "今天穿什么？", "locale": "zh-CN"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert set(body["item_refs"]) == {str(id1), str(id2)}
    snaps = {s["item_id"]: s for s in body["item_ref_snapshots"]}
    assert set(snaps) == {str(id1), str(id2)}
    # Localized name resolved for the locale; deterministic color+category
    # fallback for the other (no English leak in zh-CN).
    assert snaps[str(id1)]["name"] == "白色衬衫"
    assert snaps[str(id2)]["name"] == "黑色下装"
    assert snaps[str(id1)]["category"] == Category.TOP.value
    assert snaps[str(id2)]["color_main"] == "black"
    # Thumbnail is a signed/render URL (local backend echoes a files URL).
    assert snaps[str(id1)]["thumbnail_url"]


async def test_ask_snapshots_exclude_deleted_and_foreign(
    db_session: AsyncSession,
    patch_router_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A deleted item cited by the model is dropped from snapshots (item_refs
    still records the raw id). Foreign items can't be cited, but the batch fetch
    is user-scoped so it would drop them too."""
    user_id = uuid.uuid4()
    db_session.add(User(id=user_id))
    await db_session.flush()
    live_id = await _seed_item_with_image(db_session, user_id, title="White shirt")
    deleted_id = await _seed_item_with_image(
        db_session, user_id, title="Old tee", category=Category.TOP.value, color="grey", deleted=True
    )
    patch_router_settings(ENABLE_ASK_AIDROBE=True)
    # The wearable filter excludes the deleted item from candidates, so only the
    # live item is in the prompt at position 1; cite it.
    llm = _FakeLlm(text='{"answer": "穿这件", "referenced_items": [1]}')
    app = _structured_app(db_session, monkeypatch, llm)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/ask",
            headers={"X-User-Id": str(user_id)},
            json={"question": "今天穿什么？"},
        )
    assert resp.status_code == 200
    body = resp.json()
    snap_ids = {s["item_id"] for s in body["item_ref_snapshots"]}
    assert str(live_id) in snap_ids
    assert str(deleted_id) not in snap_ids


async def test_ask_snapshots_drop_deleted_in_history(
    db_session: AsyncSession,
    patch_router_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If an item referenced by a past Ask is later deleted, history still lists
    the raw item_ref but omits the snapshot (no per-id fetch, batch-scoped)."""
    user_id = uuid.uuid4()
    db_session.add(User(id=user_id))
    await db_session.flush()
    item_id = await _seed_item_with_image(db_session, user_id, title="White shirt")
    patch_router_settings(ENABLE_ASK_AIDROBE=True)
    llm = _FakeLlm(text='{"answer": "穿这件", "referenced_items": [1]}')
    app = _structured_app(db_session, monkeypatch, llm)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        ask_resp = await client.post(
            "/v1/ask",
            headers={"X-User-Id": str(user_id)},
            json={"question": "今天穿什么？"},
        )
        assert ask_resp.json()["item_refs"] == [str(item_id)]
        # Soft-delete the item, then read history.
        item = await db_session.get(WardrobeItem, item_id)
        assert item is not None
        item.deleted_at = datetime.now(UTC)
        await db_session.flush()
        hist = await client.get("/v1/ask/history", headers={"X-User-Id": str(user_id)})
    assert hist.status_code == 200
    rows = hist.json()
    assert len(rows) == 1
    assert rows[0]["item_refs"] == [str(item_id)]  # raw ref preserved
    assert rows[0]["item_ref_snapshots"] == []  # but no snapshot for deleted item


async def test_ask_history_snapshots_exclude_foreign_items(
    db_session: AsyncSession, patch_router_settings
) -> None:
    """A foreign item id in a row's item_refs is dropped from snapshots — the
    batch fetch is user-scoped — while the raw ref is preserved. Insert the
    AskSession directly since the normal flow can never cite another user's
    item."""
    from app.infrastructure.persistence.models.ask_session import AskSession

    owner_id = uuid.uuid4()
    db_session.add(User(id=owner_id))
    await db_session.flush()
    foreign_item_id = await _seed_item_with_image(db_session, owner_id, title="Owner shirt")

    viewer_id = uuid.uuid4()
    db_session.add(User(id=viewer_id))
    await db_session.flush()
    db_session.add(
        AskSession(
            user_id=viewer_id,
            question="谁的衣服？",
            answer="不是你的。",
            item_refs=[str(foreign_item_id)],
            context=None,
            tokens_used=1,
        )
    )
    await db_session.flush()

    patch_router_settings(ENABLE_ASK_AIDROBE=True)
    app = _build_app(
        db_session, Settings(APP_ENV="dev", ENABLE_ASK_AIDROBE=True), llm=_FakeLlm()
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get("/v1/ask/history", headers={"X-User-Id": str(viewer_id)})
    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert rows[0]["item_refs"] == [str(foreign_item_id)]  # raw ref preserved
    assert rows[0]["item_ref_snapshots"] == []  # foreign item never resolved


async def test_ask_429_when_quota_exhausted(
    db_session: AsyncSession,
    patch_router_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cost guard reads async_jobs.cost_tokens (DB-aggregate path, no Redis).
    Seed enough llm_ask token cost for today to exceed a small limit, then
    the next ask must 429 before any LLM spend."""
    user_id = await _seed_user_and_items(db_session)
    # Pre-existing usage today: 900 tokens against a 1000 limit; the projected
    # cost of the next ask (base + per-item) pushes past the budget.
    db_session.add(
        AsyncJob(
            job_type="llm_ask",
            idempotency_key=f"test:{uuid.uuid4()}",
            user_id=user_id,
            status="succeeded",
            attempts=1,
            cost_tokens=900,
            created_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    guard_settings = Settings(
        APP_ENV="dev", ENABLE_COST_GUARD=True, LLM_DAILY_TOKEN_LIMIT=1000
    )
    monkeypatch.setattr(
        "app.application.cost_guard.get_settings", lambda: guard_settings
    )
    patch_router_settings(ENABLE_ASK_AIDROBE=True)
    app = _build_app(
        db_session,
        Settings(APP_ENV="dev", ENABLE_ASK_AIDROBE=True),
        llm=_FakeLlm(),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/ask",
            headers={"X-User-Id": str(user_id)},
            json={"question": "今天穿什么？"},
        )
    assert resp.status_code == 429


async def test_ask_injects_preference_memory_when_flag_on(
    db_session: AsyncSession,
    patch_router_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BE#46 — when ENABLE_PREFERENCE_MEMORY is on, the user's durable
    {positive, negative} memory reaches the LLM prompt under `user_preferences`.
    The flag is read in answer_question.get_settings (lru-cached), so patch it."""
    from app.application.preferences.memory import SOURCE_EXPLICIT, record_preference
    from app.infrastructure.persistence.repositories.preference_memory_repo import (
        SqlAlchemyPreferenceMemoryRepository,
    )

    user_id = await _seed_user_and_items(db_session)
    await record_preference(
        SqlAlchemyPreferenceMemoryRepository(db_session),
        user_id=user_id,
        preference_type="fit",
        preference_value="oversized shirts",
        polarity="negative",
        confidence=0.95,
        source=SOURCE_EXPLICIT,
    )
    patch_router_settings(ENABLE_ASK_AIDROBE=True)
    enabled = Settings(
        APP_ENV="dev", ENABLE_ASK_AIDROBE=True, ENABLE_PREFERENCE_MEMORY=True
    )
    monkeypatch.setattr(
        "app.application.ask.answer_question.get_settings", lambda: enabled
    )
    llm = _FakeLlm()
    app = _build_app(
        db_session, Settings(APP_ENV="dev", ENABLE_ASK_AIDROBE=True), llm=llm
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/ask",
            headers={"X-User-Id": str(user_id)},
            json={"question": "今天穿什么？"},
        )
    assert resp.status_code == 200
    assert llm.last_system_prompt is not None
    assert "user_preferences" in llm.last_system_prompt
    assert "oversized shirts" in llm.last_system_prompt


async def test_ask_omits_preference_memory_when_flag_off(
    db_session: AsyncSession, patch_router_settings
) -> None:
    """Flag off: no `user_preferences` block in the prompt even if rows exist."""
    from app.application.preferences.memory import SOURCE_EXPLICIT, record_preference
    from app.infrastructure.persistence.repositories.preference_memory_repo import (
        SqlAlchemyPreferenceMemoryRepository,
    )

    user_id = await _seed_user_and_items(db_session)
    await record_preference(
        SqlAlchemyPreferenceMemoryRepository(db_session),
        user_id=user_id,
        preference_type="fit",
        preference_value="oversized shirts",
        polarity="negative",
        confidence=0.95,
        source=SOURCE_EXPLICIT,
    )
    patch_router_settings(ENABLE_ASK_AIDROBE=True)
    llm = _FakeLlm()
    app = _build_app(
        db_session, Settings(APP_ENV="dev", ENABLE_ASK_AIDROBE=True), llm=llm
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/ask",
            headers={"X-User-Id": str(user_id)},
            json={"question": "今天穿什么？"},
        )
    assert resp.status_code == 200
    assert llm.last_system_prompt is not None
    assert "user_preferences" not in llm.last_system_prompt
