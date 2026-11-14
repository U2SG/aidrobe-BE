"""Tests for GET /v1/recommendations/first-outfit (BE issue #79).

Pattern: mirrors test_recommendations_today.py exactly —
testcontainers Postgres, per-test truncate (from conftest autouse),
dependency_overrides for session + settings, ASGITransport/AsyncClient.
"""
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.recommendations.first_outfit_gate import missing_core_slots
from app.application.recommendations.generator import RecommendationGenerator
from app.common import error_codes
from app.common.enums import ItemStatus, RecommendationStatus
from app.common.local_time import local_datetime, utc_now
from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.recommendation import (
    DailyRecommendation,
    Outfit,
    OutfitFeedback,
    OutfitItem,
    WearHistory,
)
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


def _local_today() -> date:
    return local_datetime(utc_now(), None, Settings().DEFAULT_TIMEZONE).date()


async def _generate(session: AsyncSession, user_id: uuid.UUID) -> None:
    await RecommendationGenerator(
        SqlAlchemyRecommendationRepository(session)
    ).generate_for_user(
        user_id,
        _local_today(),
        "nightly_fallback",
    )


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
    """Dress + shoes is a complete first outfit and must generate successfully."""
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
    assert data["missing_slots"] == []
    assert data["state"] in {"ready", "single_outfit"}
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


async def test_today_and_first_outfit_agree_on_daily_identity(
    db_session: AsyncSession,
) -> None:
    user_id = uuid.uuid4()
    await _add_user(db_session, user_id)
    for category, color in [
        ("top", "white"),
        ("bottom", "black"),
        ("shoes", "white"),
    ]:
        await _add_item(db_session, user_id, category=category, color=color)
    await _generate(db_session, user_id)
    app = build_recommendations_app(db_session)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        today_response = await client.get(
            "/v1/recommendations/today", headers={"X-User-Id": str(user_id)}
        )
        first_outfit_response = await client.get(
            "/v1/recommendations/first-outfit", headers={"X-User-Id": str(user_id)}
        )

    assert today_response.status_code == first_outfit_response.status_code == 200
    identity_fields = (
        "recommendation_id",
        "revision",
        "generated_at",
        "generation_source",
        "trace_id",
    )
    today_data = today_response.json()["data"]
    first_outfit_data = first_outfit_response.json()["data"]
    assert {field: today_data[field] for field in identity_fields} == {
        field: first_outfit_data[field] for field in identity_fields
    }
    assert today_data["recommendation_id"]


async def test_no_outfit_gate_does_not_fabricate_recommendation_identity(
    db_session: AsyncSession,
) -> None:
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
    assert data["first_outfit"] is None
    assert data["recommendation_id"] is None
    assert data["revision"] is None
    assert data["generated_at"] is None
    assert data["generation_source"] is None
    assert data["trace_id"] is None


@pytest.mark.parametrize("count", [1, 2])
async def test_state_derivation_n1_and_n2(count: int, db_session: AsyncSession) -> None:
    from app.application.recommendations.first_outfit_gate import FirstOutfitGate

    user_id = uuid.uuid4()
    today = _local_today()
    await _add_user(db_session, user_id)
    items = [
        await _add_item(db_session, user_id, category=category)
        for category in ("top", "bottom", "shoes")
    ]
    repository = SqlAlchemyRecommendationRepository(db_session)
    daily = await repository.archive_current_and_revise(
        DailyRecommendation(
            user_id=user_id,
            recommendation_date=today,
            status=RecommendationStatus.READY.value,
            generation_source="test",
        )
    )
    now = datetime.now(UTC)
    for index, outfit_type in enumerate(("safe", "fast")[:count], start=1):
        outfit = await repository.add_outfit(
            Outfit(
                daily_recommendation_id=daily.id,
                user_id=user_id,
                outfit_type=outfit_type,
                title=outfit_type,
                reason_text="test",
                score_total=Decimal(index),
                rank_order=index,
                time_sensitivity="standard",
                created_at=now,
            )
        )
        for order, item in enumerate(items):
            await repository.add_outfit_item(
                OutfitItem(outfit_id=outfit.id, item_id=item.id, role=item.category, display_order=order, created_at=now)
            )

    result = await FirstOutfitGate(repository).execute(user_id, today)

    assert result.state == ("single_outfit" if count == 1 else "ready")
    assert result.feasible_outfit_count == count
    assert result.first_outfit is not None
    assert result.first_outfit.outfit_type == "safe"
    assert not result.generated


@pytest.mark.parametrize("stale_status", ["insufficient_items", "ready"])
async def test_first_outfit_replaces_stale_daily_without_losing_history(
    stale_status: str, db_session: AsyncSession
) -> None:
    user_id = uuid.uuid4()
    today = _local_today()
    await _add_user(db_session, user_id)
    top = await _add_item(db_session, user_id, category="top", color="white")
    bottom = await _add_item(db_session, user_id, category="bottom")
    shoes = (
        await _add_item(db_session, user_id, category="shoes")
        if stale_status == "ready"
        else None
    )
    repository = SqlAlchemyRecommendationRepository(db_session)
    await _generate(db_session, user_id)
    previous = await repository.get_daily(user_id, today)
    assert previous is not None and previous.status == stale_status
    previous_id = previous.id
    old_outfit_id = None
    if shoes is not None:
        old_outfit_id = previous.outfits[0].id
        await repository.add_outfit_favorite(user_id, old_outfit_id)
        await repository.add_feedback(
            OutfitFeedback(
                user_id=user_id, outfit_id=old_outfit_id, feedback_type="dislike", created_at=datetime.now(UTC)
            )
        )
        db_session.add(
            WearHistory(
                user_id=user_id,
                outfit_id=old_outfit_id,
                worn_date=today,
                selected_at=datetime.now(UTC),
                created_at=datetime.now(UTC),
            )
        )
        shoes.status = ItemStatus.UNAVAILABLE.value
        await db_session.flush()
    current_shoes = await _add_item(db_session, user_id, category="shoes", color="white")
    app = build_recommendations_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.get("/v1/recommendations/first-outfit", headers={"X-User-Id": str(user_id)})

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["state"] == "single_outfit"
    assert data["generation_source"] == "read_time"
    assert data["recommendation_id"] != str(previous_id)
    assert {row["item_id"] for row in data["first_outfit"]["items"]} == {
        str(top.id), str(bottom.id), str(current_shoes.id)
    }
    assert not previous.is_current
    assert await repository.count_current_dailies(user_id, today) == 1
    assert await repository.count_revisions_for_date(user_id, today) == 1
    if old_outfit_id is not None:
        assert await repository.list_favorited_outfit_ids(user_id, [old_outfit_id]) == {old_outfit_id}
        assert await repository.get_feedback_for_user(old_outfit_id, user_id) is not None
        assert await repository.get_wear_history(old_outfit_id, user_id, today) is not None


async def test_first_outfit_timeout_returns_technical_error(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.application.recommendations.background import schedule_today_recompute
    from app.infrastructure.persistence.models.async_job import AsyncJob

    user_id = uuid.uuid4()
    await _add_user(db_session, user_id)
    for category in ("top", "bottom", "shoes"):
        await _add_item(db_session, user_id, category=category)
    repository = SqlAlchemyRecommendationRepository(db_session)
    await schedule_today_recompute(user_id, repository)

    async def timed_out_generation(*args: object, **kwargs: object) -> None:
        # Expire the current budget without any wall-clock sleep.
        raise TimeoutError

    monkeypatch.setattr(RecommendationGenerator, "generate_for_user", timed_out_generation)
    app = build_recommendations_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.get("/v1/recommendations/first-outfit", headers={"X-User-Id": str(user_id)})

    assert response.status_code == 504
    assert response.json()["code"] == error_codes.INTERNAL_ERROR
    assert response.json()["data"] is None
    assert await repository.get_daily(user_id, _local_today()) is None
    job_status = await db_session.scalar(select(AsyncJob.status).where(AsyncJob.user_id == user_id))
    assert job_status == "queued"


async def test_image_unavailable_blocks_untrustworthy_first_outfit(
    db_session: AsyncSession,
) -> None:
    """A first outfit with no identifiable item images must not be declared ready."""
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
    assert data["state"] == "no_outfit"
    assert data["feasible_outfit_count"] == 0
    assert data["gate_failures"] == ["image_unavailable"]
    assert data["first_outfit"] is None


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
