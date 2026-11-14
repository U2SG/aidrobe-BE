"""Tests for V3 M11: outfit history & OOTD endpoints."""

import uuid
from datetime import UTC, date, datetime

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.recommendation import (
    Outfit,
    OutfitFeedback,
    OutfitItem,
    WearHistory,
)
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import outfit_history as history_router
from app.presentation.middlewares.exception_handler import setup_exception_handlers


def build_history_app(session: AsyncSession) -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)

    async def override_db() -> AsyncSession:
        return session

    def override_settings() -> Settings:
        return Settings(APP_ENV="dev")

    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[get_settings] = override_settings
    app.include_router(history_router.router)
    return app


async def seed_wear_history(
    session: AsyncSession, user_id: uuid.UUID, worn_date: date
) -> WearHistory:
    from sqlalchemy import select as sa_select

    from app.infrastructure.persistence.models.recommendation import DailyRecommendation

    # Only add User if not already present (multiple calls with same user_id)
    existing = await session.scalar(sa_select(User).where(User.id == user_id))
    if existing is None:
        session.add(User(id=user_id))
        await session.flush()

    daily = DailyRecommendation(
        user_id=user_id,
        recommendation_date=worn_date,
        status="ready",
        generation_source="manual",
    )
    session.add(daily)
    await session.flush()

    outfit = Outfit(
        daily_recommendation_id=daily.id,
        user_id=user_id,
        outfit_type="safe",
        title="Test outfit",
        reason_text="Looks good",
        score_total=0.8,
        rank_order=1,
        created_at=datetime.now(UTC),
    )
    session.add(outfit)
    await session.flush()

    history = WearHistory(
        user_id=user_id,
        outfit_id=outfit.id,
        worn_date=worn_date,
        selected_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )
    session.add(history)
    await session.flush()
    return history


async def test_list_outfit_history_returns_entries(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    await seed_wear_history(db_session, user_id, date(2026, 5, 1))
    await seed_wear_history(db_session, user_id, date(2026, 5, 2))
    app = build_history_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get("/v1/outfit-history", headers={"X-User-Id": str(user_id)})

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2
    # Ordered by worn_date desc
    assert body["items"][0]["worn_date"] >= body["items"][1]["worn_date"]


async def test_list_outfit_history_pagination(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    for day in range(1, 6):
        await seed_wear_history(db_session, user_id, date(2026, 5, day))
    app = build_history_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get(
            "/v1/outfit-history?page=1&page_size=2",
            headers={"X-User-Id": str(user_id)},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 5
    assert len(body["items"]) == 2


async def test_get_outfit_history_by_date(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    await seed_wear_history(db_session, user_id, date(2026, 5, 10))
    app = build_history_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get(
            "/v1/outfit-history/2026-05-10",
            headers={"X-User-Id": str(user_id)},
        )

    assert resp.status_code == 200
    assert resp.json()["worn_date"] == "2026-05-10"


async def test_get_outfit_history_by_date_not_found(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    db_session.add(User(id=user_id))
    await db_session.flush()
    app = build_history_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get(
            "/v1/outfit-history/2099-01-01",
            headers={"X-User-Id": str(user_id)},
        )

    assert resp.status_code == 404


async def test_calendar_returns_dates(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    await seed_wear_history(db_session, user_id, date(2026, 5, 1))
    await seed_wear_history(db_session, user_id, date(2026, 5, 15))
    app = build_history_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get(
            "/v1/outfit-history/calendar?year=2026&month=5",
            headers={"X-User-Id": str(user_id)},
        )

    assert resp.status_code == 200
    dates = resp.json()["dates"]
    assert "2026-05-01" in dates
    assert "2026-05-15" in dates


async def test_upload_ootd(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    history = await seed_wear_history(db_session, user_id, date(2026, 5, 5))
    app = build_history_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.post(
            f"/v1/outfit-history/{history.id}/ootd",
            headers={"X-User-Id": str(user_id)},
            json={"ootd_image_url": "https://example.com/ootd.jpg"},
        )

    assert resp.status_code == 200
    assert resp.json()["ootd_image_url"] == "https://example.com/ootd.jpg"
    await db_session.refresh(history)
    assert history.ootd_image_url == "https://example.com/ootd.jpg"


async def seed_outfit_items(
    session: AsyncSession, user_id: uuid.UUID, outfit_id: uuid.UUID
) -> list[WardrobeItem]:
    items = [
        WardrobeItem(user_id=user_id, title="White tee", category="top", color_main="white"),
        WardrobeItem(user_id=user_id, title="Blue jeans", category="bottom", color_main="blue"),
    ]
    session.add_all(items)
    await session.flush()
    for order, item in enumerate(items):
        session.add(
            OutfitItem(
                outfit_id=outfit_id,
                item_id=item.id,
                role=item.category,
                display_order=order,
                created_at=datetime.now(UTC),
            )
        )
    await session.flush()
    return items


async def test_list_outfit_history_includes_items_and_reason_tags(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    history = await seed_wear_history(db_session, user_id, date(2026, 5, 1))
    items = await seed_outfit_items(db_session, user_id, history.outfit_id)
    db_session.add(
        OutfitFeedback(
            user_id=user_id,
            outfit_id=history.outfit_id,
            feedback_type="dislike",
            reason_tags={"tags": ["too_formal"]},
            created_at=datetime.now(UTC),
        )
    )
    await db_session.flush()
    app = build_history_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get("/v1/outfit-history", headers={"X-User-Id": str(user_id)})

    assert resp.status_code == 200
    entry = resp.json()["items"][0]
    assert [i["item_id"] for i in entry["outfit"]["items"]] == [str(i.id) for i in items]
    assert [i["title"] for i in entry["outfit"]["items"]] == ["White tee", "Blue jeans"]
    assert [i["display_role"] for i in entry["outfit"]["items"]] == ["top", "bottom"]
    assert "cover_image_url" in entry["outfit"]["items"][0]
    assert entry["feedback_type"] == "dislike"
    assert entry["reason_tags"] == ["too_formal"]


async def test_get_outfit_history_by_date_includes_items_and_feedback(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    history = await seed_wear_history(db_session, user_id, date(2026, 5, 10))
    await seed_outfit_items(db_session, user_id, history.outfit_id)
    # A bare list is accepted as well as the stored {"tags": [...]} shape.
    db_session.add(
        OutfitFeedback(
            user_id=user_id,
            outfit_id=history.outfit_id,
            feedback_type="dislike",
            reason_tags=["wrong_fit"],  # type: ignore[arg-type]
            created_at=datetime.now(UTC),
        )
    )
    await db_session.flush()
    app = build_history_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get("/v1/outfit-history/2026-05-10", headers={"X-User-Id": str(user_id)})

    assert resp.status_code == 200
    body = resp.json()
    assert len(body["outfit"]["items"]) == 2
    assert body["reason_tags"] == ["wrong_fit"]


async def test_get_outfit_history_by_date_two_outfits_same_day_returns_latest(
    db_session: AsyncSession,
) -> None:
    user_id = uuid.uuid4()
    first = await seed_wear_history(db_session, user_id, date(2026, 5, 10))
    first_outfit = await db_session.get(Outfit, first.outfit_id)
    assert first_outfit is not None
    second_outfit = Outfit(
        daily_recommendation_id=first_outfit.daily_recommendation_id,
        user_id=user_id,
        outfit_type="safe",
        title="Evening outfit",
        reason_text="Dinner",
        score_total=0.7,
        rank_order=2,
        created_at=datetime.now(UTC),
    )
    db_session.add(second_outfit)
    await db_session.flush()
    db_session.add(
        WearHistory(
            user_id=user_id,
            outfit_id=second_outfit.id,
            worn_date=date(2026, 5, 10),
            selected_at=datetime.now(UTC),
            created_at=datetime.now(UTC),
        )
    )
    await db_session.flush()
    app = build_history_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get("/v1/outfit-history/2026-05-10", headers={"X-User-Id": str(user_id)})

    assert resp.status_code == 200
    assert resp.json()["outfit"]["title"] == "Evening outfit"
