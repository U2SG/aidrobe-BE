"""Tests for V3 M11: outfit history & OOTD endpoints."""

import uuid
from datetime import UTC, date, datetime

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.recommendation import Outfit, WearHistory
from app.infrastructure.persistence.models.user import User
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
