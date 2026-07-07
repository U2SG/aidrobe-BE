"""Tests for GET /v1/recommendations/first-outfit (BE issue #79).

Pattern: mirrors test_recommendations_today.py exactly —
testcontainers Postgres, per-test truncate (from conftest autouse),
dependency_overrides for session + settings, ASGITransport/AsyncClient.
"""
import uuid
from datetime import date

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.recommendations.first_outfit_gate import missing_core_slots
from app.application.recommendations.generator import RecommendationGenerator
from app.common.enums import ItemStatus
from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import recommendations as recommendations_router
from app.presentation.middlewares.exception_handler import setup_exception_handlers

# ---------------------------------------------------------------------------
# App factory (mirrors test_recommendations_today.py)
# ---------------------------------------------------------------------------


def build_recommendations_app(session: AsyncSession) -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)

    async def override_db_session() -> AsyncSession:
        return session

    def override_settings() -> Settings:
        return Settings(APP_ENV="dev")

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = override_settings
    app.include_router(recommendations_router.router)
    return app


# ---------------------------------------------------------------------------
# Seed helpers
# ---------------------------------------------------------------------------


async def _add_user(session: AsyncSession, user_id: uuid.UUID) -> None:
    session.add(User(id=user_id))
    await session.flush()


async def _add_item(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    category: str,
    color: str = "black",
    cover_image_url: str | None = "https://example.com/img.jpg",
) -> WardrobeItem:
    item = WardrobeItem(
        user_id=user_id,
        title=f"{category} item",
        category=category,
        color_main=color,
        status=ItemStatus.WEARABLE.value,
        cover_image_url=cover_image_url,
    )
    session.add(item)
    await session.flush()
    return item


async def _generate(session: AsyncSession, user_id: uuid.UUID) -> None:
    await RecommendationGenerator(
        SqlAlchemyRecommendationRepository(session)
    ).generate_for_user(user_id, date.today(), "test")


# ---------------------------------------------------------------------------
# Unit tests for missing_core_slots (pure function)
# ---------------------------------------------------------------------------


def test_missing_core_slots_empty_wardrobe() -> None:
    """Empty wardrobe: neither path started → report separates path."""
    assert missing_core_slots([]) == ["top", "bottom", "shoes"]


def test_missing_core_slots_separates_complete() -> None:
    items = [
        WardrobeItem(category="top", color_main="white", title="t", user_id=uuid.uuid4(), status="wearable"),
        WardrobeItem(category="bottom", color_main="black", title="t", user_id=uuid.uuid4(), status="wearable"),
        WardrobeItem(category="shoes", color_main="white", title="t", user_id=uuid.uuid4(), status="wearable"),
    ]
    assert missing_core_slots(items) == []


def test_missing_core_slots_dress_path_complete() -> None:
    items = [
        WardrobeItem(category="dress", color_main="red", title="t", user_id=uuid.uuid4(), status="wearable"),
        WardrobeItem(category="shoes", color_main="white", title="t", user_id=uuid.uuid4(), status="wearable"),
    ]
    assert missing_core_slots(items) == []


def test_missing_core_slots_top_and_shoes_no_bottom() -> None:
    """Separates path started (top+shoes), missing bottom."""
    items = [
        WardrobeItem(category="top", color_main="white", title="t", user_id=uuid.uuid4(), status="wearable"),
        WardrobeItem(category="shoes", color_main="white", title="t", user_id=uuid.uuid4(), status="wearable"),
    ]
    assert missing_core_slots(items) == ["bottom"]


def test_missing_core_slots_dress_no_shoes() -> None:
    """Dress path started, missing only shoes — closer than separates."""
    items = [
        WardrobeItem(category="dress", color_main="red", title="t", user_id=uuid.uuid4(), status="wearable"),
    ]
    result = missing_core_slots(items)
    assert result == ["shoes"]


def test_missing_core_slots_dress_closer_than_separates() -> None:
    """User has dress (1 missing: shoes) AND top (2 missing: bottom+shoes) → dress path wins."""
    items = [
        WardrobeItem(category="dress", color_main="red", title="t", user_id=uuid.uuid4(), status="wearable"),
        WardrobeItem(category="top", color_main="white", title="t", user_id=uuid.uuid4(), status="wearable"),
    ]
    result = missing_core_slots(items)
    assert result == ["shoes"]


def test_missing_core_slots_only_shoes() -> None:
    """Only shoes — separates path started (has shoes)."""
    items = [
        WardrobeItem(category="shoes", color_main="white", title="t", user_id=uuid.uuid4(), status="wearable"),
    ]
    result = missing_core_slots(items)
    # Separates path has shoes → missing top+bottom.
    # Dress path has shoes → missing dress (1 missing).
    # Dress path is closer (fewer missing) or tie → dress path reported.
    assert result == ["dress"]


# ---------------------------------------------------------------------------
# API integration tests
# ---------------------------------------------------------------------------


async def test_empty_wardrobe_returns_no_outfit(db_session: AsyncSession) -> None:
    """Empty wardrobe → no_outfit, missing_slots covers separates path, gate_failures has missing_* entries."""
    user_id = uuid.uuid4()
    await _add_user(db_session, user_id)
    app = build_recommendations_app(db_session)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get(
            "/v1/recommendations/first-outfit", headers={"X-User-Id": str(user_id)}
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["state"] == "no_outfit"
    assert data["feasible_outfit_count"] == 0
    assert set(data["missing_slots"]) == {"top", "bottom", "shoes"}
    assert "missing_top" in data["gate_failures"]
    assert "missing_bottom" in data["gate_failures"]
    assert "missing_shoes" in data["gate_failures"]
    assert data["first_outfit"] is None


async def test_partial_wardrobe_top_and_shoes_missing_bottom(db_session: AsyncSession) -> None:
    """Top + shoes, no bottom → no_outfit, missing_slots == ["bottom"]."""
    user_id = uuid.uuid4()
    await _add_user(db_session, user_id)
    await _add_item(db_session, user_id, category="top")
    await _add_item(db_session, user_id, category="shoes")
    app = build_recommendations_app(db_session)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get(
            "/v1/recommendations/first-outfit", headers={"X-User-Id": str(user_id)}
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["state"] == "no_outfit"
    assert data["missing_slots"] == ["bottom"]
    assert data["gate_failures"] == ["missing_bottom"]
    assert data["first_outfit"] is None


async def test_dress_and_shoes_passes_slot_check(db_session: AsyncSession) -> None:
    """Dress + shoes → slot check passes; state derived from generation outcome."""
    user_id = uuid.uuid4()
    await _add_user(db_session, user_id)
    await _add_item(db_session, user_id, category="dress", color="red")
    await _add_item(db_session, user_id, category="shoes", color="beige")
    app = build_recommendations_app(db_session)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get(
            "/v1/recommendations/first-outfit", headers={"X-User-Id": str(user_id)}
        )

    assert response.status_code == 200
    data = response.json()["data"]
    # Slot check passes; state is engine-derived (ready or single_outfit or
    # engine_insufficient — all are valid depending on the engine's output for
    # a 2-item wardrobe). What matters: missing_slots is empty.
    assert data["missing_slots"] == []
    # If the engine succeeded, first_outfit is present.
    if data["state"] != "no_outfit":
        assert data["first_outfit"] is not None


async def test_full_wardrobe_returns_ready_or_single_outfit(db_session: AsyncSession) -> None:
    """Wardrobe with top + bottom + shoes (multiple) → ready or single_outfit; first_outfit present."""
    user_id = uuid.uuid4()
    await _add_user(db_session, user_id)
    # Seed with enough variety for the engine to build >=1 outfit
    for cat, color in [
        ("top", "white"),
        ("top", "blue"),
        ("bottom", "black"),
        ("bottom", "grey"),
        ("shoes", "white"),
    ]:
        await _add_item(db_session, user_id, category=cat, color=color)
    app = build_recommendations_app(db_session)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get(
            "/v1/recommendations/first-outfit", headers={"X-User-Id": str(user_id)}
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["state"] in {"ready", "single_outfit"}
    assert data["feasible_outfit_count"] >= 1
    assert data["missing_slots"] == []
    assert data["first_outfit"] is not None
    assert data["first_outfit"]["outfit_id"]


@pytest.mark.parametrize("n_outfits_hint", [1, 2])
async def test_state_derivation_n1_and_n2(n_outfits_hint: int, db_session: AsyncSession) -> None:
    """Unit-level check for n→state derivation inside FirstOutfitGate.execute.

    We can't reliably fixture exactly-1 outfit through the real engine because
    the combination count depends on wardrobe depth. Instead we test the
    derivation function in isolation by patching the repository to return a
    synthetic DailyRecommendation with n Outfit rows.
    """
    from datetime import UTC, datetime
    from decimal import Decimal
    from unittest.mock import AsyncMock

    from app.application.recommendations.first_outfit_gate import FirstOutfitGate
    from app.common.enums import RecommendationStatus
    from app.infrastructure.persistence.models.recommendation import DailyRecommendation, Outfit

    user_id = uuid.uuid4()
    today = date.today()

    # Build synthetic outfit rows
    def make_outfit(outfit_type: str, score: float) -> Outfit:
        o = Outfit(
            daily_recommendation_id=uuid.uuid4(),
            user_id=user_id,
            outfit_type=outfit_type,
            title=f"Test {outfit_type}",
            reason_text="test",
            score_total=Decimal(str(score)),
            rank_order=1,
            time_sensitivity="standard",
            created_at=datetime.now(UTC),
        )
        o.id = uuid.uuid4()
        o.items = []
        return o

    outfits = [make_outfit("safe", 0.8)] if n_outfits_hint == 1 else [
        make_outfit("safe", 0.8),
        make_outfit("fast", 0.6),
    ]

    daily = DailyRecommendation(
        user_id=user_id,
        recommendation_date=today,
        status=RecommendationStatus.READY.value,
        generation_source="test",
    )
    daily.id = uuid.uuid4()
    daily.outfits = outfits

    # Synthetic wearable items (slots complete so slot check passes)
    def make_wardrobe_item(category: str) -> WardrobeItem:
        item = WardrobeItem(
            user_id=user_id,
            title=category,
            category=category,
            color_main="black",
            status="wearable",
            cover_image_url="https://example.com/img.jpg",
        )
        item.id = uuid.uuid4()
        item.images = []
        return item

    wearable_items = [make_wardrobe_item(c) for c in ["top", "bottom", "shoes"]]

    repo = AsyncMock(spec=SqlAlchemyRecommendationRepository)
    repo.get_wearable_items.return_value = wearable_items
    repo.get_daily.return_value = daily

    result = await FirstOutfitGate(repo).execute(user_id, today)
    expected_state = "single_outfit" if n_outfits_hint == 1 else "ready"
    assert result.state == expected_state
    assert result.feasible_outfit_count == n_outfits_hint
    assert result.first_outfit is not None


async def test_image_unavailable_flag_does_not_block(db_session: AsyncSession) -> None:
    """Items without cover_image_url and no image rows → gate_failures has 'image_unavailable',
    but state is not 'no_outfit' (FE has SVG fallback)."""
    user_id = uuid.uuid4()
    await _add_user(db_session, user_id)
    # cover_image_url=None and no images table rows
    for cat, color in [
        ("top", "white"),
        ("top", "blue"),
        ("bottom", "black"),
        ("shoes", "white"),
    ]:
        await _add_item(db_session, user_id, category=cat, color=color, cover_image_url=None)
    app = build_recommendations_app(db_session)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get(
            "/v1/recommendations/first-outfit", headers={"X-User-Id": str(user_id)}
        )

    assert response.status_code == 200
    data = response.json()["data"]
    # Slot check passes; engine may succeed or fail. We care:
    if data["state"] in {"ready", "single_outfit"}:
        assert "image_unavailable" in data["gate_failures"]
        assert data["first_outfit"] is not None
    else:
        # Engine couldn't build — engine_insufficient is expected
        assert "engine_insufficient" in data["gate_failures"]


async def test_today_route_unaffected(db_session: AsyncSession) -> None:
    """Regression: /today still works correctly after the gate endpoint is added."""
    user_id = uuid.uuid4()
    await _add_user(db_session, user_id)
    for cat, color in [("top", "white"), ("bottom", "black"), ("shoes", "white")]:
        await _add_item(db_session, user_id, category=cat, color=color)
    await _generate(db_session, user_id)
    app = build_recommendations_app(db_session)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get(
            "/v1/recommendations/today", headers={"X-User-Id": str(user_id)}
        )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "ready"
