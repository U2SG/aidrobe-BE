"""M15 Ask Aidrobe — flag gate, happy path (fake LLM), history, quota 429."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports.llm import LlmAnswer
from app.bootstrap.container import llm_service
from app.common.enums import Category, ItemStatus
from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import ask as ask_router
from app.presentation.middlewares.exception_handler import setup_exception_handlers


class _FakeLlm:
    """Deterministic stand-in for LlmPort.ask — no HTTP, fixed tokens."""

    def __init__(self, *, text: str = "建议搭配白衬衫和黑裤子。", tokens: int = 42) -> None:
        self._text = text
        self._tokens = tokens
        self.last_system_prompt: str | None = None

    async def polish_reason(self, prompt: str) -> str:
        return ""

    async def ask(self, system_prompt: str, question: str) -> LlmAnswer:
        self.last_system_prompt = system_prompt
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


async def test_ask_happy_path(
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
            json={"question": "今天有客户会，穿哪套更稳？", "context": {"scene": "business"}},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["answer"] == "建议搭配白衬衫和黑裤子。"
    assert body["tokens_used"] == 42
    # v1: references the items fed into the prompt (both wearable items).
    assert len(body["item_refs"]) == 2
    assert uuid.UUID(body["ask_session_id"])


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


async def test_ask_429_when_quota_exhausted(
    db_session: AsyncSession,
    patch_router_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cost guard reads async_jobs.cost_tokens (DB-aggregate path, no Redis).
    Seed enough llm_ask token cost for today to exceed a small limit, then
    the next ask must 429 before any LLM spend."""
    user_id = await _seed_user_and_items(db_session)
    # Pre-existing usage today: 900 tokens against a 1000 limit; projected
    # cost of the next ask (1500) blows the budget.
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
