"""Phase D1 — daily_recommendations.context_json + /today response gating."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports.weather import WeatherInfo
from app.application.recommendations.generator import RecommendationGenerator
from app.common.enums import Category, ItemStatus
from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.recommendation import DailyRecommendation
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import recommendations as recommendations_router
from app.presentation.middlewares.exception_handler import setup_exception_handlers


class _StaticWeather:
    """Deterministic stand-in. The recommendation engine just reads fields off
    `WeatherInfo`, so a fake without HTTP is enough for context-snapshot
    plumbing tests."""

    async def get_weather(self, city: str) -> WeatherInfo:
        return WeatherInfo(
            temperature_c=18.5,
            feels_like_c=17.0,
            rain_probability=0.4,
            condition="cloudy",
        )

    async def get_today(self, location: str) -> WeatherInfo:
        return await self.get_weather(location)


def _build_app(session: AsyncSession, settings_override: Settings) -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)

    async def override_db_session() -> AsyncSession:
        return session

    def override_settings() -> Settings:
        return settings_override

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = override_settings
    app.include_router(recommendations_router.router)
    return app


async def _seed_user_and_items(session: AsyncSession) -> uuid.UUID:
    user_id = uuid.uuid4()
    session.add(User(id=user_id, location="Beijing"))
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
    return user_id


async def test_generator_persists_context_when_weather_available(
    db_session: AsyncSession,
) -> None:
    """When the weather port returns a snapshot, the generator stores it on
    the daily row regardless of feature flag — flag only gates exposure."""
    user_id = await _seed_user_and_items(db_session)
    repo = SqlAlchemyRecommendationRepository(db_session)
    await RecommendationGenerator(repo, weather_service=_StaticWeather()).generate_for_user(
        user_id, date.today(), "manual"
    )
    daily = await repo.get_daily(user_id, date.today())
    assert daily is not None
    ctx: Any = daily.context_json
    assert isinstance(ctx, dict)
    assert ctx["weather"]["temperature_c"] == 18.5
    assert ctx["weather"]["condition"] == "cloudy"
    assert "fetched_at" in ctx


async def test_generator_persists_null_context_without_weather(
    db_session: AsyncSession,
) -> None:
    """No weather port + no scene → no snapshot. NULL is meaningfully
    different from `{}` so future explainability can tell them apart."""
    user_id = await _seed_user_and_items(db_session)
    repo = SqlAlchemyRecommendationRepository(db_session)
    await RecommendationGenerator(repo).generate_for_user(user_id, date.today(), "manual")
    daily = await repo.get_daily(user_id, date.today())
    assert daily is not None
    assert daily.context_json is None


async def test_today_response_omits_context_when_flag_off(db_session: AsyncSession) -> None:
    user_id = await _seed_user_and_items(db_session)
    repo = SqlAlchemyRecommendationRepository(db_session)
    await RecommendationGenerator(repo, weather_service=_StaticWeather()).generate_for_user(
        user_id, date.today(), "manual"
    )

    app = _build_app(db_session, Settings(APP_ENV="dev", ENABLE_TODAY_CONTEXT=False))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get(
            "/v1/recommendations/today", headers={"X-User-Id": str(user_id)}
        )
    assert resp.status_code == 200
    body = resp.json()["data"]
    # Field present in schema but None when flag is off.
    assert body.get("context") is None


async def test_today_response_exposes_context_when_flag_on(db_session: AsyncSession) -> None:
    user_id = await _seed_user_and_items(db_session)
    repo = SqlAlchemyRecommendationRepository(db_session)
    await RecommendationGenerator(repo, weather_service=_StaticWeather()).generate_for_user(
        user_id, date.today(), "manual"
    )

    app = _build_app(db_session, Settings(APP_ENV="dev", ENABLE_TODAY_CONTEXT=True))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get(
            "/v1/recommendations/today", headers={"X-User-Id": str(user_id)}
        )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["context"] is not None
    assert body["context"]["weather"]["temperature_c"] == 18.5
    assert body["context"]["weather"]["rain_probability"] == 0.4
    assert body["context"]["fetched_at"]


async def test_today_response_handles_legacy_rows_without_context(
    db_session: AsyncSession,
) -> None:
    """A daily row written before Phase D landed has context_json=NULL.
    The flag-on path must not raise on those rows."""
    user_id = await _seed_user_and_items(db_session)
    # Build a daily WITHOUT going through the generator so context_json stays NULL.
    db_session.add(
        DailyRecommendation(
            user_id=user_id,
            recommendation_date=date.today(),
            status="ready",
            generation_source="manual",
            context_json=None,
        )
    )
    await db_session.commit()

    app = _build_app(db_session, Settings(APP_ENV="dev", ENABLE_TODAY_CONTEXT=True))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get(
            "/v1/recommendations/today", headers={"X-User-Id": str(user_id)}
        )
    assert resp.status_code == 200
    assert resp.json()["data"]["context"] is None


async def test_today_response_drops_malformed_weather_chunk(
    db_session: AsyncSession,
) -> None:
    """If a future schema change leaves persisted weather in an old shape, the
    presentation layer drops the malformed sub-field instead of 500-ing."""
    user_id = await _seed_user_and_items(db_session)
    db_session.add(
        DailyRecommendation(
            user_id=user_id,
            recommendation_date=date.today(),
            status="ready",
            generation_source="manual",
            context_json={
                "weather": {"unknown_field_only": True},  # missing required fields
                "scene": "commute",
                "fetched_at": "2026-05-07T00:00:00+00:00",
            },
        )
    )
    await db_session.commit()

    app = _build_app(db_session, Settings(APP_ENV="dev", ENABLE_TODAY_CONTEXT=True))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get(
            "/v1/recommendations/today", headers={"X-User-Id": str(user_id)}
        )
    assert resp.status_code == 200
    body = resp.json()["data"]
    assert body["context"] is not None
    assert body["context"]["weather"] is None  # malformed chunk dropped
    assert body["context"]["scene"] == "commute"
    assert body["context"]["fetched_at"] == "2026-05-07T00:00:00+00:00"
