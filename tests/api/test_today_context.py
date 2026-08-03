"""Phase D1 — daily_recommendations.context_json + /today response gating."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports.weather import WeatherInfo
from app.application.recommendations.generator import RecommendationGenerator
from app.common.enums import Category, ItemStatus
from app.common.local_time import local_datetime, utc_now
from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.recommendation import DailyRecommendation
from app.infrastructure.persistence.models.user import User, UserSettings
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import recommendations as recommendations_router
from app.presentation.middlewares.exception_handler import setup_exception_handlers


def _local_today() -> date:
    return local_datetime(utc_now(), None, Settings().DEFAULT_TIMEZONE).date()


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

    async def get_forecast(self, location: str, target_date: date) -> WeatherInfo | None:
        # BE#126 — today's generation now asks for the day outlook first.
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


@pytest.fixture
def patched_settings_in_router(monkeypatch: pytest.MonkeyPatch):
    """The recommendations router calls `get_settings()` directly inside
    `_build_context_response` (lru-cached, bypasses FastAPI dep override).
    Tests that flip ENABLE_TODAY_CONTEXT must patch the module-level
    `get_settings` — matches the codebase pattern in cost_guard / user_purge."""
    def _factory(**overrides: Any):
        merged = Settings(APP_ENV="dev", **overrides)
        monkeypatch.setattr(
            "app.presentation.api.v1.routers.recommendations.get_settings",
            lambda: merged,
        )
        return merged
    return _factory


async def _seed_user_and_items(session: AsyncSession) -> uuid.UUID:
    user_id = uuid.uuid4()
    session.add(User(id=user_id))
    # location lives on UserSettings, not User — needed because the
    # generator reads location to feed the weather port.
    session.add(UserSettings(user_id=user_id, location="Beijing"))
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
        user_id, _local_today(), "manual"
    )
    daily = await repo.get_daily(user_id, _local_today())
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
    await RecommendationGenerator(repo).generate_for_user(user_id, _local_today(), "manual")
    daily = await repo.get_daily(user_id, _local_today())
    assert daily is not None
    assert daily.context_json is None


async def test_today_response_omits_context_when_flag_off(db_session: AsyncSession) -> None:
    user_id = await _seed_user_and_items(db_session)
    repo = SqlAlchemyRecommendationRepository(db_session)
    await RecommendationGenerator(repo, weather_service=_StaticWeather()).generate_for_user(
        user_id, _local_today(), "manual"
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


async def test_today_response_exposes_context_when_flag_on(
    db_session: AsyncSession, patched_settings_in_router: Any
) -> None:
    user_id = await _seed_user_and_items(db_session)
    repo = SqlAlchemyRecommendationRepository(db_session)
    await RecommendationGenerator(repo, weather_service=_StaticWeather()).generate_for_user(
        user_id, _local_today(), "manual"
    )

    patched_settings_in_router(ENABLE_TODAY_CONTEXT=True)
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
    db_session: AsyncSession, patched_settings_in_router: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A daily row written before Phase D landed has context_json=NULL.
    The flag-on path must not raise on those rows."""
    user_id = await _seed_user_and_items(db_session)
    # Build a daily WITHOUT going through the generator so context_json stays NULL.
    db_session.add(
        DailyRecommendation(
            user_id=user_id,
            recommendation_date=_local_today(),
            status="ready",
            generation_source="manual",
            context_json=None,
        )
    )
    await db_session.commit()

    # No weather provider → the live backfill is a no-op, so this exercises the
    # legacy-row presentation path in isolation.
    monkeypatch.setattr(
        "app.presentation.api.v1.routers.recommendations.weather_service", lambda: None
    )
    patched_settings_in_router(ENABLE_TODAY_CONTEXT=True)
    app = _build_app(db_session, Settings(APP_ENV="dev", ENABLE_TODAY_CONTEXT=True))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get(
            "/v1/recommendations/today", headers={"X-User-Id": str(user_id)}
        )
    assert resp.status_code == 200
    assert resp.json()["data"]["context"] is None


async def test_generator_persists_null_context_when_weather_provider_raises(
    db_session: AsyncSession,
) -> None:
    """Weather provider failure must NOT abort the generation. The daily
    row still lands; context_json stays None (caller can retry on next
    refresh). This is the fail-soft contract /today depends on — a flaky
    weather API doesn't take recommendations down."""

    class _ExplodingWeather:
        async def get_weather(self, city: str) -> Any:
            raise RuntimeError("upstream 503")

        async def get_today(self, location: str) -> Any:
            raise RuntimeError("upstream 503")

    user_id = await _seed_user_and_items(db_session)
    repo = SqlAlchemyRecommendationRepository(db_session)
    daily = await RecommendationGenerator(
        repo, weather_service=_ExplodingWeather()
    ).generate_for_user(user_id, _local_today(), "manual")
    assert daily is not None
    assert daily.context_json is None  # provider exception swallowed
    # And the row IS persisted — not aborted.
    fresh = await repo.get_daily(user_id, _local_today())
    assert fresh is not None


async def test_today_response_drops_malformed_weather_chunk(
    db_session: AsyncSession, patched_settings_in_router: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If a future schema change leaves persisted weather in an old shape, the
    presentation layer drops the malformed sub-field instead of 500-ing."""
    user_id = await _seed_user_and_items(db_session)
    db_session.add(
        DailyRecommendation(
            user_id=user_id,
            recommendation_date=_local_today(),
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

    # No provider → dropped weather stays dropped (no live backfill in this test).
    monkeypatch.setattr(
        "app.presentation.api.v1.routers.recommendations.weather_service", lambda: None
    )
    patched_settings_in_router(ENABLE_TODAY_CONTEXT=True)
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


async def test_today_backfills_weather_when_snapshot_missing_it(
    db_session: AsyncSession, patched_settings_in_router: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Snapshot has no weather (e.g. generated before the user set a location),
    but the user now has a location → GET /today fetches it live and backfills
    the response, so weather shows immediately without waiting for cron or
    spending a /today/refresh slot."""
    user_id = await _seed_user_and_items(db_session)  # location="Beijing"
    repo = SqlAlchemyRecommendationRepository(db_session)
    # Generate WITHOUT a weather port → context_json is NULL.
    await RecommendationGenerator(repo).generate_for_user(user_id, _local_today(), "manual")

    monkeypatch.setattr(
        "app.presentation.api.v1.routers.recommendations.weather_service",
        lambda: _StaticWeather(),
    )
    patched_settings_in_router(ENABLE_TODAY_CONTEXT=True)
    app = _build_app(db_session, Settings(APP_ENV="dev", ENABLE_TODAY_CONTEXT=True))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get(
            "/v1/recommendations/today", headers={"X-User-Id": str(user_id)}
        )
    assert resp.status_code == 200
    weather = resp.json()["data"]["context"]["weather"]
    assert weather is not None
    assert weather["temperature_c"] == 18.5
    assert weather["condition"] == "cloudy"


async def test_today_no_weather_backfill_without_location(
    db_session: AsyncSession, patched_settings_in_router: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No location set → nothing to query, so the backfill is skipped even
    though a provider is available (context stays None for a null snapshot)."""
    user_id = await _seed_user_and_items(db_session)
    settings_row = await db_session.scalar(
        select(UserSettings).where(UserSettings.user_id == user_id)
    )
    assert settings_row is not None
    settings_row.location = None
    await db_session.flush()
    repo = SqlAlchemyRecommendationRepository(db_session)
    await RecommendationGenerator(repo).generate_for_user(user_id, _local_today(), "manual")

    # Provider IS available; if the backfill ignored the missing location it
    # would populate weather and fail the assertion below.
    monkeypatch.setattr(
        "app.presentation.api.v1.routers.recommendations.weather_service",
        lambda: _StaticWeather(),
    )
    patched_settings_in_router(ENABLE_TODAY_CONTEXT=True)
    app = _build_app(db_session, Settings(APP_ENV="dev", ENABLE_TODAY_CONTEXT=True))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get(
            "/v1/recommendations/today", headers={"X-User-Id": str(user_id)}
        )
    assert resp.status_code == 200
    assert resp.json()["data"]["context"] is None
