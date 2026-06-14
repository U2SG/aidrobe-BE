"""Outfit Calendar (V4) API tests — flag gating, CRUD, calendar aggregation, wear.

Self-built app with get_db_session overridden to the testcontainers session,
mirroring tests/api/test_ootd_poster.py. The router calls get_settings()
directly (lru-cached, bypasses the dep override), so the flag is monkeypatched
on the consuming module.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings
from app.infrastructure.persistence.models.recommendation import (
    DailyRecommendation,
    Outfit,
    WearHistory,
)
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import outfit_plan
from app.presentation.middlewares.exception_handler import setup_exception_handlers


def _build_app(session: AsyncSession) -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)

    async def _db() -> AsyncSession:
        return session

    app.dependency_overrides[get_db_session] = _db
    app.include_router(outfit_plan.router)
    return app


def _enable_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        outfit_plan,
        "get_settings",
        lambda: Settings(APP_ENV="dev", ENABLE_OUTFIT_CALENDAR=True),
    )


async def _seed_user_outfit(
    session: AsyncSession, user_id: uuid.UUID, on_date: date
) -> Outfit:
    existing = await session.scalar(select(User).where(User.id == user_id))
    if existing is None:
        session.add(User(id=user_id))
        await session.flush()

    daily = DailyRecommendation(
        user_id=user_id,
        recommendation_date=on_date,
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
    return outfit


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


async def test_flag_off_returns_503(db_session: AsyncSession) -> None:
    app = _build_app(db_session)
    async with _client(app) as client:
        resp = await client.post(
            "/v1/outfit-plans",
            headers={"X-User-Id": str(uuid.uuid4())},
            json={"outfit_id": str(uuid.uuid4()), "planned_date": "2026-06-20"},
        )
    assert resp.status_code == 503


async def test_create_plan(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_flag(monkeypatch)
    user_id = uuid.uuid4()
    outfit = await _seed_user_outfit(db_session, user_id, date(2026, 6, 20))
    app = _build_app(db_session)
    async with _client(app) as client:
        resp = await client.post(
            "/v1/outfit-plans",
            headers={"X-User-Id": str(user_id)},
            json={"outfit_id": str(outfit.id), "planned_date": "2026-06-20"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["outfit_plan_id"]
    assert body["status"] == "planned"
    assert body["planned_date"] == "2026-06-20"


async def test_create_plan_duplicate_returns_409(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_flag(monkeypatch)
    user_id = uuid.uuid4()
    outfit = await _seed_user_outfit(db_session, user_id, date(2026, 6, 20))
    app = _build_app(db_session)
    async with _client(app) as client:
        payload = {"outfit_id": str(outfit.id), "planned_date": "2026-06-20"}
        first = await client.post(
            "/v1/outfit-plans", headers={"X-User-Id": str(user_id)}, json=payload
        )
        assert first.status_code == 200, first.text
        dup = await client.post(
            "/v1/outfit-plans", headers={"X-User-Id": str(user_id)}, json=payload
        )
    assert dup.status_code == 409, dup.text


async def test_calendar_aggregates_worn_and_planned(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_flag(monkeypatch)
    user_id = uuid.uuid4()
    # planned outfit on the 20th
    planned_outfit = await _seed_user_outfit(db_session, user_id, date(2026, 6, 20))
    # worn outfit (separate outfit) -> WearHistory on the 21st
    worn_outfit = await _seed_user_outfit(db_session, user_id, date(2026, 6, 21))
    db_session.add(
        WearHistory(
            user_id=user_id,
            outfit_id=worn_outfit.id,
            worn_date=date(2026, 6, 21),
            selected_at=datetime.now(UTC),
            created_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    app = _build_app(db_session)
    async with _client(app) as client:
        # create the plan via API
        cp = await client.post(
            "/v1/outfit-plans",
            headers={"X-User-Id": str(user_id)},
            json={"outfit_id": str(planned_outfit.id), "planned_date": "2026-06-20"},
        )
        assert cp.status_code == 200, cp.text
        cal = await client.get(
            "/v1/outfit-plans/calendar?start=2026-06-01&end=2026-06-30",
            headers={"X-User-Id": str(user_id)},
        )
    assert cal.status_code == 200, cal.text
    days = {d["date"]: d for d in cal.json()["days"]}
    assert days["2026-06-20"]["cells"][0]["kind"] == "planned"
    assert days["2026-06-20"]["cells"][0]["outfit_plan_id"]
    assert days["2026-06-21"]["cells"][0]["kind"] == "worn"
    assert days["2026-06-21"]["cells"][0]["wear_history_id"]


async def test_reschedule_and_cancel(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_flag(monkeypatch)
    user_id = uuid.uuid4()
    outfit = await _seed_user_outfit(db_session, user_id, date(2026, 6, 20))
    app = _build_app(db_session)
    async with _client(app) as client:
        cp = await client.post(
            "/v1/outfit-plans",
            headers={"X-User-Id": str(user_id)},
            json={"outfit_id": str(outfit.id), "planned_date": "2026-06-20"},
        )
        plan_id = cp.json()["outfit_plan_id"]

        # reschedule
        re = await client.patch(
            f"/v1/outfit-plans/{plan_id}",
            headers={"X-User-Id": str(user_id)},
            json={"planned_date": "2026-06-22"},
        )
        assert re.status_code == 200, re.text
        assert re.json()["planned_date"] == "2026-06-22"

        # cancel
        ca = await client.patch(
            f"/v1/outfit-plans/{plan_id}",
            headers={"X-User-Id": str(user_id)},
            json={"status": "cancelled"},
        )
        assert ca.status_code == 200, ca.text
        assert ca.json()["status"] == "cancelled"


async def test_patch_invalid_status_returns_422(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_flag(monkeypatch)
    user_id = uuid.uuid4()
    outfit = await _seed_user_outfit(db_session, user_id, date(2026, 6, 20))
    app = _build_app(db_session)
    async with _client(app) as client:
        cp = await client.post(
            "/v1/outfit-plans",
            headers={"X-User-Id": str(user_id)},
            json={"outfit_id": str(outfit.id), "planned_date": "2026-06-20"},
        )
        plan_id = cp.json()["outfit_plan_id"]
        resp = await client.patch(
            f"/v1/outfit-plans/{plan_id}",
            headers={"X-User-Id": str(user_id)},
            json={"status": "bogus"},
        )
    assert resp.status_code == 422


async def test_mark_worn_lands_wear_history_and_cancels_plan(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_flag(monkeypatch)
    user_id = uuid.uuid4()
    outfit = await _seed_user_outfit(db_session, user_id, date(2026, 6, 20))
    app = _build_app(db_session)
    async with _client(app) as client:
        cp = await client.post(
            "/v1/outfit-plans",
            headers={"X-User-Id": str(user_id)},
            json={"outfit_id": str(outfit.id), "planned_date": "2026-06-20"},
        )
        plan_id = cp.json()["outfit_plan_id"]
        worn = await client.post(
            f"/v1/outfit-plans/{plan_id}/wear",
            headers={"X-User-Id": str(user_id)},
        )
    assert worn.status_code == 200, worn.text
    body = worn.json()
    assert body["wear_history_id"]
    assert body["status"] == "cancelled"
    assert body["worn_date"] == "2026-06-20"

    # WearHistory landed
    wh = await db_session.scalar(
        select(WearHistory).where(
            WearHistory.user_id == user_id, WearHistory.outfit_id == outfit.id
        )
    )
    assert wh is not None
    assert wh.worn_date == date(2026, 6, 20)
