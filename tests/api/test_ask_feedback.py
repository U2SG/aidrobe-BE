"""BE#86 — AskResponse stable identifiers and Ask feedback endpoint."""

from __future__ import annotations

import uuid

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings
from app.infrastructure.persistence.models.recommendation import RecommendationEvent
from tests.api.test_ask import (
    _build_app,
    _FakeLlm,
    _seed_focused_item,
    _seed_user_and_items,
    _structured_app,
)


@pytest.fixture
def patch_router_settings(monkeypatch: pytest.MonkeyPatch):
    """Mirror of test_ask.patch_router_settings — patches the module-level
    get_settings used by _gate() (lru-cached, bypasses dep override)."""

    def _factory(**overrides) -> Settings:  # type: ignore[no-untyped-def]
        merged = Settings(APP_ENV="dev", **overrides)
        monkeypatch.setattr(
            "app.presentation.api.v1.routers.ask.get_settings", lambda: merged
        )
        return merged

    return _factory

# ---------------------------------------------------------------------------
# recommendation_id population on AskResponse
# ---------------------------------------------------------------------------


async def test_ask_response_recommendation_id_set_when_item_refs_nonempty(
    db_session: AsyncSession,
    patch_router_settings,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Global ask with structured output ON + citing LLM → recommendation_id
    equals ask_session_id; source_surface is always 'ask'."""
    user_id = await _seed_user_and_items(db_session)
    patch_router_settings(ENABLE_ASK_AIDROBE=True)
    # Cite position 1 so item_refs will be non-empty.
    llm = _FakeLlm(text='{"answer": "穿白衬衫吧", "referenced_items": [1]}')
    app = _structured_app(db_session, monkeypatch, llm)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/ask",
            headers={"X-User-Id": str(user_id)},
            json={"question": "今天穿什么？"},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert body["item_refs"]  # non-empty
    assert body["recommendation_id"] == body["ask_session_id"]
    assert body["source_surface"] == "ask"


async def test_ask_response_recommendation_id_none_for_pure_text(
    db_session: AsyncSession,
    patch_router_settings,
) -> None:
    """User with no wardrobe items → item_refs empty → recommendation_id None."""
    # Seed a bare user with no wardrobe items.
    user_id = uuid.uuid4()
    from app.infrastructure.persistence.models.user import User

    db_session.add(User(id=user_id))
    await db_session.flush()

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
    assert resp.status_code == 200
    body = resp.json()
    assert body["item_refs"] == []
    assert body["recommendation_id"] is None
    assert body["source_surface"] == "ask"


async def test_ask_focused_item_appears_in_item_refs_and_recommendation_id_set(
    db_session: AsyncSession,
    patch_router_settings,
) -> None:
    """Focused-item ask (structured OFF) → focused item is sole ref →
    recommendation_id is set."""
    user_id = await _seed_user_and_items(db_session)
    focus_id = await _seed_focused_item(db_session, user_id)
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
            json={"question": "用这件搭一套", "focused_item_id": str(focus_id)},
        )
    assert resp.status_code == 200
    body = resp.json()
    assert str(focus_id) in body["item_refs"]
    assert body["recommendation_id"] == body["ask_session_id"]


# ---------------------------------------------------------------------------
# Feedback endpoint
# ---------------------------------------------------------------------------


async def test_ask_feedback_happy_path_writes_event(
    db_session: AsyncSession,
    patch_router_settings,
) -> None:
    """POST /v1/ask/{id}/feedback returns 200 and writes a RecommendationEvent
    with source='ask_feedback', recommendation_id=None, source_surface='ask'."""
    user_id = await _seed_user_and_items(db_session)
    focus_id = await _seed_focused_item(db_session, user_id)
    patch_router_settings(ENABLE_ASK_AIDROBE=True)
    app = _build_app(
        db_session,
        Settings(APP_ENV="dev", ENABLE_ASK_AIDROBE=True),
        llm=_FakeLlm(),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        # Create a focused-item session so focused_item_id lands in context.
        ask_resp = await client.post(
            "/v1/ask",
            headers={"X-User-Id": str(user_id)},
            json={"question": "用这件搭一套", "focused_item_id": str(focus_id)},
        )
        assert ask_resp.status_code == 200
        ask_session_id = ask_resp.json()["ask_session_id"]

        fb_resp = await client.post(
            f"/v1/ask/{ask_session_id}/feedback",
            headers={"X-User-Id": str(user_id)},
            json={"feedback_type": "love", "reason_tags": ["comfy"]},
        )
    assert fb_resp.status_code == 200
    assert fb_resp.json() == {"success": True}

    # Verify the event row written to the DB.
    event = await db_session.scalar(
        select(RecommendationEvent).where(
            RecommendationEvent.user_id == user_id,
            RecommendationEvent.source == "ask_feedback",
        )
    )
    assert event is not None
    assert event.recommendation_id is None
    assert event.outfit_id is None
    assert event.event_metadata is not None
    assert event.event_metadata["source_surface"] == "ask"
    assert event.event_metadata["ask_session_id"] == ask_session_id
    assert event.event_metadata["reason_tags"] == ["comfy"]
    # focused_item_id must be carried into the event metadata.
    assert event.event_metadata["focused_item_id"] == str(focus_id)
    # Cited items land under "item_ids" — the exact key feedback-learning's
    # _signature() reads — so Ask feedback actually feeds suppression learning.
    assert event.event_metadata["item_ids"] == [str(focus_id)]

    from app.application.recommendations.feedback_learning import _signature

    assert _signature(event.event_metadata) == frozenset({str(focus_id)})


async def test_ask_feedback_on_another_users_session_returns_404(
    db_session: AsyncSession,
    patch_router_settings,
) -> None:
    """Feedback on a session owned by another user → 404 (no existence reveal)."""
    owner_id = await _seed_user_and_items(db_session)
    attacker_id = uuid.uuid4()
    from app.infrastructure.persistence.models.user import User

    db_session.add(User(id=attacker_id))
    await db_session.flush()

    patch_router_settings(ENABLE_ASK_AIDROBE=True)
    app = _build_app(
        db_session,
        Settings(APP_ENV="dev", ENABLE_ASK_AIDROBE=True),
        llm=_FakeLlm(),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        # Owner creates a session.
        ask_resp = await client.post(
            "/v1/ask",
            headers={"X-User-Id": str(owner_id)},
            json={"question": "今天穿什么？"},
        )
        assert ask_resp.status_code == 200
        ask_session_id = ask_resp.json()["ask_session_id"]

        # Attacker tries to leave feedback on owner's session.
        fb_resp = await client.post(
            f"/v1/ask/{ask_session_id}/feedback",
            headers={"X-User-Id": str(attacker_id)},
            json={"feedback_type": "like"},
        )
    assert fb_resp.status_code == 404


async def test_ask_feedback_on_nonexistent_session_returns_404(
    db_session: AsyncSession,
    patch_router_settings,
) -> None:
    """Feedback on a random UUID that has no session row → 404."""
    user_id = await _seed_user_and_items(db_session)
    patch_router_settings(ENABLE_ASK_AIDROBE=True)
    app = _build_app(
        db_session,
        Settings(APP_ENV="dev", ENABLE_ASK_AIDROBE=True),
        llm=_FakeLlm(),
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        fb_resp = await client.post(
            f"/v1/ask/{uuid.uuid4()}/feedback",
            headers={"X-User-Id": str(user_id)},
            json={"feedback_type": "dislike"},
        )
    assert fb_resp.status_code == 404
