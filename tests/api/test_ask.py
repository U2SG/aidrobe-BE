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
from app.common.enums import Category, ImageType, ItemStatus
from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem, WardrobeItemImage
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
    assert llm.last_json_mode is True  # provider constrained to JSON mode


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
    # Localized name resolved for the locale; title fallback for the other.
    assert snaps[str(id1)]["name"] == "白色衬衫"
    assert snaps[str(id2)]["name"] == "Black pants"
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
