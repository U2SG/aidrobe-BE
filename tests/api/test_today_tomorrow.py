"""BE#77 — date-aware Today: tomorrow mode generates fresh, never a stale copy."""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.local_time import local_datetime, utc_now
from app.config.settings import Settings
from app.infrastructure.persistence.models.user import UserSettings
from tests.api.test_recommendations_today import (
    build_recommendations_app,
    seed_ready_recommendation,
)


def _local_today() -> date:
    return local_datetime(utc_now(), None, Settings().DEFAULT_TIMEZONE).date()


@pytest.fixture
def date_aware_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    merged = Settings(APP_ENV="dev", ENABLE_DATE_AWARE_TODAY=True)
    monkeypatch.setattr(
        "app.presentation.api.v1.routers.recommendations.get_settings", lambda: merged
    )


async def test_tomorrow_generates_fresh_not_stale_today(
    db_session: AsyncSession, date_aware_settings: None
) -> None:
    user_id = uuid.uuid4()
    # Seeds the wardrobe AND today's recommendation — the exact stale copy the
    # contract forbids reusing for tomorrow.
    await seed_ready_recommendation(db_session, user_id)
    app = build_recommendations_app(db_session)
    tomorrow = _local_today() + timedelta(days=1)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.get(
            f"/v1/recommendations/today?target_date={tomorrow.isoformat()}",
            headers={"X-User-Id": str(user_id)},
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["recommendation_date"] == tomorrow.isoformat()
    assert data["mode"] == "tomorrow"
    assert data["status"] == "ready"
    assert data["outfits"]


async def test_target_date_beyond_tomorrow_rejected(
    db_session: AsyncSession, date_aware_settings: None
) -> None:
    user_id = uuid.uuid4()
    await seed_ready_recommendation(db_session, user_id)
    app = build_recommendations_app(db_session)
    day_after = _local_today() + timedelta(days=2)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.get(
            f"/v1/recommendations/today?target_date={day_after.isoformat()}",
            headers={"X-User-Id": str(user_id)},
        )

    assert response.status_code == 400


async def test_target_date_uses_user_local_date_across_utc_midnight(
    db_session: AsyncSession,
    date_aware_settings: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid.uuid4()
    await seed_ready_recommendation(db_session, user_id)
    db_session.add(UserSettings(user_id=user_id, timezone="Asia/Singapore"))
    await db_session.flush()
    # Local time is 2026-07-16 00:30, while UTC is still 2026-07-15.
    monkeypatch.setattr(
        "app.presentation.api.v1.routers.recommendations.utc_now",
        lambda: datetime(2026, 7, 15, 16, 30, tzinfo=UTC),
    )
    app = build_recommendations_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.get(
            "/v1/recommendations/today?target_date=2026-07-17",
            headers={"X-User-Id": str(user_id)},
        )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["recommendation_date"] == "2026-07-17"
    assert response.json()["data"]["mode"] == "tomorrow"


async def test_flag_off_ignores_target_date(db_session: AsyncSession) -> None:
    # Backward compatibility: without ENABLE_DATE_AWARE_TODAY the param is
    # ignored and legacy today behavior (incl. mode=None) is preserved.
    user_id = uuid.uuid4()
    await seed_ready_recommendation(db_session, user_id)
    app = build_recommendations_app(db_session)
    tomorrow = _local_today() + timedelta(days=1)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.get(
            f"/v1/recommendations/today?target_date={tomorrow.isoformat()}",
            headers={"X-User-Id": str(user_id)},
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["recommendation_date"] == _local_today().isoformat()
    assert data["mode"] is None
    assert data["can_refresh"] is False
    assert data["can_context_override"] is False
