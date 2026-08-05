import uuid
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.recommendations.generator import RecommendationGenerator
from app.common.enums import Category, ItemStatus
from app.common.local_time import local_datetime, utc_now
from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import recommendations as recommendations_router
from app.presentation.middlewares.exception_handler import setup_exception_handlers


def _default_local_today() -> date:
    return local_datetime(utc_now(), None, Settings().DEFAULT_TIMEZONE).date()


def build_recommendations_app(
    session: AsyncSession,
    settings_override: Settings | None = None,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)
    settings = settings_override or Settings(APP_ENV="dev")

    async def override_db_session() -> AsyncSession:
        return session

    def override_settings() -> Settings:
        return settings

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = override_settings
    if monkeypatch is not None:
        monkeypatch.setattr(recommendations_router, "get_settings", lambda: settings)
    app.include_router(recommendations_router.router)
    return app


async def seed_ready_recommendation(session: AsyncSession, user_id: uuid.UUID) -> None:
    session.add(User(id=user_id))
    await session.flush()
    for title, category, color in [
        ("White shirt", Category.TOP.value, "white"),
        ("Blue shirt", Category.TOP.value, "blue"),
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
    await RecommendationGenerator(SqlAlchemyRecommendationRepository(session)).generate_for_user(
        user_id, _default_local_today(), "nightly_fallback"
    )


async def test_existing_ready_recommendation_returns_immediately(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    await seed_ready_recommendation(db_session, user_id)
    app = build_recommendations_app(db_session)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get(
            "/v1/recommendations/today", headers={"X-User-Id": str(user_id)}
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "ready"
    assert data["outfits"]
    assert data["recommendation_id"]
    assert data["revision"] == 1


async def test_today_reread_keeps_stable_identity_and_revision(
    db_session: AsyncSession,
) -> None:
    user_id = uuid.uuid4()
    await seed_ready_recommendation(db_session, user_id)
    app = build_recommendations_app(db_session)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        first = await client.get(
            "/v1/recommendations/today", headers={"X-User-Id": str(user_id)}
        )
        second = await client.get(
            "/v1/recommendations/today", headers={"X-User-Id": str(user_id)}
        )

    assert first.status_code == second.status_code == 200
    identity_fields = (
        "recommendation_id",
        "revision",
        "generated_at",
        "generation_source",
        "trace_id",
    )
    first_data = first.json()["data"]
    second_data = second.json()["data"]
    assert {field: first_data[field] for field in identity_fields} == {
        field: second_data[field] for field in identity_fields
    }
    assert first_data["recommendation_id"]
    assert first_data["revision"] == 1
    assert first_data["generated_at"]
    assert first_data["generation_source"] == "nightly"
    assert first_data["trace_id"] is None


async def seed_tops_only_recommendation(session: AsyncSession, user_id: uuid.UUID) -> None:
    session.add(User(id=user_id))
    await session.flush()
    for title, color in [("White shirt", "white"), ("Blue shirt", "blue"), ("Gray shirt", "gray")]:
        session.add(
            WardrobeItem(
                user_id=user_id,
                title=title,
                category=Category.TOP.value,
                color_main=color,
                status=ItemStatus.WEARABLE.value,
            )
        )
    await session.flush()
    await RecommendationGenerator(SqlAlchemyRecommendationRepository(session)).generate_for_user(
        user_id, _default_local_today(), "nightly_fallback"
    )


async def test_today_and_tomorrow_use_distinct_recommendation_identities(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid.uuid4()
    await seed_ready_recommendation(db_session, user_id)
    tomorrow = _default_local_today() + timedelta(days=1)
    await RecommendationGenerator(SqlAlchemyRecommendationRepository(db_session)).generate_for_user(
        user_id, tomorrow, "tomorrow_preview"
    )
    settings = Settings(APP_ENV="dev", ENABLE_DATE_AWARE_TODAY=True)
    app = build_recommendations_app(db_session, settings, monkeypatch)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        today_response = await client.get(
            "/v1/recommendations/today", headers={"X-User-Id": str(user_id)}
        )
        tomorrow_response = await client.get(
            "/v1/recommendations/today",
            params={"target_date": tomorrow.isoformat()},
            headers={"X-User-Id": str(user_id)},
        )

    assert today_response.status_code == tomorrow_response.status_code == 200
    today_data = today_response.json()["data"]
    tomorrow_data = tomorrow_response.json()["data"]
    assert today_data["recommendation_date"] == _default_local_today().isoformat()
    assert tomorrow_data["recommendation_date"] == tomorrow.isoformat()
    assert today_data["recommendation_id"] != tomorrow_data["recommendation_id"]
    assert tomorrow_data["generation_source"] == "tomorrow_preview"


async def test_persisted_insufficient_daily_still_carries_identity(
    db_session: AsyncSession,
) -> None:
    user_id = uuid.uuid4()
    db_session.add(User(id=user_id))
    await db_session.flush()
    await RecommendationGenerator(SqlAlchemyRecommendationRepository(db_session)).generate_for_user(
        user_id, _default_local_today(), "nightly_fallback"
    )
    app = build_recommendations_app(db_session)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get(
            "/v1/recommendations/today", headers={"X-User-Id": str(user_id)}
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "insufficient_items"
    assert data["recommendation_id"]
    assert data["revision"] == 1
    assert data["generated_at"]
    assert data["generation_source"] == "nightly"
    assert data["trace_id"] is None


async def test_ready_with_only_tops_surfaces_coverage_gap_hint(db_session: AsyncSession) -> None:
    # Tops but no bottoms: still "ready" (one stacked look), yet the wardrobe is
    # structurally unable to combine. The insufficient-items banner never fires
    # here, so the response must carry a nudge to add the missing category.
    user_id = uuid.uuid4()
    await seed_tops_only_recommendation(db_session, user_id)
    app = build_recommendations_app(db_session)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get(
            "/v1/recommendations/today", headers={"X-User-Id": str(user_id)}
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "ready"
    assert data["guidance_text"]
    assert "下装" in data["guidance_text"]


async def test_missing_today_returns_insufficient_not_generating(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    app = build_recommendations_app(db_session)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get(
            "/v1/recommendations/today", headers={"X-User-Id": str(user_id)}
        )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == "insufficient_items"
    assert response.json()["data"]["status"] != "generating"
    assert response.json()["data"]["guidance_text"]
    job = await db_session.scalar(
        select(AsyncJob).where(
            AsyncJob.user_id == user_id,
            AsyncJob.job_type == "recommendation_recompute",
        )
    )
    assert job is not None
    assert job.status == "queued"
    assert await SqlAlchemyRecommendationRepository(db_session).get_daily(
        user_id, _default_local_today()
    ) is None
