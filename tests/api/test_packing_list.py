"""Packing list (M16) API tests — flag gating, generate, get/list, patch, rules.

Self-built app with get_db_session overridden to the testcontainers session,
mirroring tests/api/test_outfit_plan.py. The router calls get_settings()
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

from app.application.packing import generate as packing_generate
from app.config.settings import Settings
from app.infrastructure.persistence.models.packing_list import PackingList
from app.infrastructure.persistence.models.recommendation import (
    DailyRecommendation,
    Outfit,
    OutfitItem,
    WearHistory,
)
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import packing_list
from app.presentation.middlewares.exception_handler import setup_exception_handlers


def _build_app(session: AsyncSession) -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)

    async def _db() -> AsyncSession:
        return session

    app.dependency_overrides[get_db_session] = _db
    app.include_router(packing_list.router)
    return app


def _enable_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        packing_list,
        "get_settings",
        lambda: Settings(APP_ENV="dev", ENABLE_PACKING_LIST=True),
    )


async def _ensure_user(session: AsyncSession, user_id: uuid.UUID) -> None:
    existing = await session.scalar(select(User).where(User.id == user_id))
    if existing is None:
        session.add(User(id=user_id))
        await session.flush()


async def _seed_item(
    session: AsyncSession,
    user_id: uuid.UUID,
    category: str,
    *,
    fit_status: str = "fits_well",
    status: str = "wearable",
) -> WardrobeItem:
    item = WardrobeItem(
        user_id=user_id,
        title=f"{category} item",
        category=category,
        color_main="black",
        status=status,
        fit_status=fit_status,
    )
    session.add(item)
    await session.flush()
    return item


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


async def test_flag_off_returns_503(db_session: AsyncSession) -> None:
    app = _build_app(db_session)
    async with _client(app) as client:
        resp = await client.post(
            "/v1/packing-lists",
            headers={"X-User-Id": str(uuid.uuid4())},
            json={"destination": "Shanghai", "days": 2, "scenes": ["business"]},
        )
    assert resp.status_code == 503


async def test_generate_picks_wearable_items(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_flag(monkeypatch)
    user_id = uuid.uuid4()
    await _ensure_user(db_session, user_id)
    top = await _seed_item(db_session, user_id, "top")
    bottom = await _seed_item(db_session, user_id, "bottom")
    shoes = await _seed_item(db_session, user_id, "shoes")

    app = _build_app(db_session)
    async with _client(app) as client:
        resp = await client.post(
            "/v1/packing-lists",
            headers={"X-User-Id": str(user_id)},
            json={
                "destination": "Shanghai",
                "days": 2,
                "scenes": ["dinner"],
                "weather_hint": "cool",
                "departure_date": "2026-07-01",
            },
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["packing_list_id"]
    assert body["status"] == "draft"
    assert body["days"] == 2
    assert body["scenes"] == ["dinner"]
    picked_ids = {item["item_id"] for item in body["items"]}
    assert picked_ids == {str(top.id), str(bottom.id), str(shoes.id)}
    # 2 days x 3 categories (top/bottom/shoes) = 6 selections
    assert len(body["items"]) == 6
    assert {item["day"] for item in body["items"]} == {1, 2}


async def test_generate_excludes_non_wearable(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_flag(monkeypatch)
    user_id = uuid.uuid4()
    await _ensure_user(db_session, user_id)
    wearable_top = await _seed_item(db_session, user_id, "top")
    await _seed_item(db_session, user_id, "top", status="archived")

    app = _build_app(db_session)
    async with _client(app) as client:
        resp = await client.post(
            "/v1/packing-lists",
            headers={"X-User-Id": str(user_id)},
            json={"days": 1, "scenes": ["dinner"]},
        )
    assert resp.status_code == 200, resp.text
    top_ids = {item["item_id"] for item in resp.json()["items"] if item["scene"] == "dinner"}
    assert str(wearable_top.id) in top_ids
    assert len(top_ids) >= 1


async def test_generate_skips_recently_worn(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_flag(monkeypatch)
    user_id = uuid.uuid4()
    await _ensure_user(db_session, user_id)
    recent_top = await _seed_item(db_session, user_id, "top")
    fresh_top = await _seed_item(db_session, user_id, "top")

    # Build an outfit that wore `recent_top` 3 days before departure.
    departure = date(2026, 7, 10)
    daily = DailyRecommendation(
        user_id=user_id,
        recommendation_date=departure,
        status="ready",
        generation_source="manual",
    )
    db_session.add(daily)
    await db_session.flush()
    outfit = Outfit(
        daily_recommendation_id=daily.id,
        user_id=user_id,
        outfit_type="safe",
        title="Worn outfit",
        reason_text="x",
        score_total=0.5,
        rank_order=1,
        created_at=datetime.now(UTC),
    )
    db_session.add(outfit)
    await db_session.flush()
    db_session.add(
        OutfitItem(
            outfit_id=outfit.id,
            item_id=recent_top.id,
            role="top",
            display_order=1,
            created_at=datetime.now(UTC),
        )
    )
    db_session.add(
        WearHistory(
            user_id=user_id,
            outfit_id=outfit.id,
            worn_date=date(2026, 7, 7),
            selected_at=datetime.now(UTC),
            created_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    app = _build_app(db_session)
    async with _client(app) as client:
        resp = await client.post(
            "/v1/packing-lists",
            headers={"X-User-Id": str(user_id)},
            json={"days": 1, "scenes": ["dinner"], "departure_date": "2026-07-10"},
        )
    assert resp.status_code == 200, resp.text
    picked_ids = {item["item_id"] for item in resp.json()["items"]}
    assert str(recent_top.id) not in picked_ids
    assert str(fresh_top.id) in picked_ids


async def test_get_and_list(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_flag(monkeypatch)
    user_id = uuid.uuid4()
    await _ensure_user(db_session, user_id)
    await _seed_item(db_session, user_id, "top")

    app = _build_app(db_session)
    async with _client(app) as client:
        created = await client.post(
            "/v1/packing-lists",
            headers={"X-User-Id": str(user_id)},
            json={"days": 1, "scenes": ["dinner"]},
        )
        pl_id = created.json()["packing_list_id"]

        detail = await client.get(
            f"/v1/packing-lists/{pl_id}", headers={"X-User-Id": str(user_id)}
        )
        assert detail.status_code == 200, detail.text
        assert detail.json()["packing_list_id"] == pl_id

        history = await client.get(
            "/v1/packing-lists", headers={"X-User-Id": str(user_id)}
        )
        assert history.status_code == 200, history.text
        assert any(pl["packing_list_id"] == pl_id for pl in history.json())


async def test_get_not_found_returns_404(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_flag(monkeypatch)
    user_id = uuid.uuid4()
    await _ensure_user(db_session, user_id)
    app = _build_app(db_session)
    async with _client(app) as client:
        resp = await client.get(
            f"/v1/packing-lists/{uuid.uuid4()}", headers={"X-User-Id": str(user_id)}
        )
    assert resp.status_code == 404


async def test_patch_replaces_items_and_confirms(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_flag(monkeypatch)
    user_id = uuid.uuid4()
    await _ensure_user(db_session, user_id)
    top = await _seed_item(db_session, user_id, "top")
    replacement = await _seed_item(db_session, user_id, "top")

    app = _build_app(db_session)
    async with _client(app) as client:
        created = await client.post(
            "/v1/packing-lists",
            headers={"X-User-Id": str(user_id)},
            json={"days": 1, "scenes": ["dinner"]},
        )
        pl_id = created.json()["packing_list_id"]

        patched = await client.patch(
            f"/v1/packing-lists/{pl_id}",
            headers={"X-User-Id": str(user_id)},
            json={
                "items": [
                    {
                        "item_id": str(replacement.id),
                        "day": 1,
                        "scene": "dinner",
                        "notes": "swapped",
                    }
                ],
                "status": "confirmed",
            },
        )
    assert patched.status_code == 200, patched.text
    body = patched.json()
    assert body["status"] == "confirmed"
    assert len(body["items"]) == 1
    assert body["items"][0]["item_id"] == str(replacement.id)
    assert str(top.id) not in {item["item_id"] for item in body["items"]}

    # persisted
    stored = await db_session.scalar(
        select(PackingList).where(PackingList.id == uuid.UUID(pl_id))
    )
    assert stored is not None
    assert stored.status == "confirmed"


async def test_patch_invalid_status_returns_422(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_flag(monkeypatch)
    user_id = uuid.uuid4()
    await _ensure_user(db_session, user_id)
    await _seed_item(db_session, user_id, "top")
    app = _build_app(db_session)
    async with _client(app) as client:
        created = await client.post(
            "/v1/packing-lists",
            headers={"X-User-Id": str(user_id)},
            json={"days": 1, "scenes": ["dinner"]},
        )
        pl_id = created.json()["packing_list_id"]
        resp = await client.patch(
            f"/v1/packing-lists/{pl_id}",
            headers={"X-User-Id": str(user_id)},
            json={"status": "bogus"},
        )
    assert resp.status_code == 422


def _enable_planner(monkeypatch: pytest.MonkeyPatch) -> None:
    # Router gate (ENABLE_PACKING_LIST) + the use case's planner flag live in two
    # different modules' lru-cached get_settings; patch both.
    settings = Settings(APP_ENV="dev", ENABLE_PACKING_LIST=True, ENABLE_PACKING_PLANNER=True)
    monkeypatch.setattr(packing_list, "get_settings", lambda: settings)
    monkeypatch.setattr(packing_generate, "get_settings", lambda: settings)


async def test_planner_returns_persisted_plan_with_daily_outfits_and_missing_items(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_planner(monkeypatch)
    user_id = uuid.uuid4()
    await _ensure_user(db_session, user_id)
    # Top/bottom/shoes enough for outfits; light shoes + no outerwear → warnings.
    owned = {
        str((await _seed_item(db_session, user_id, "top")).id),
        str((await _seed_item(db_session, user_id, "bottom")).id),
    }
    shoes = await _seed_item(db_session, user_id, "shoes")
    shoes.color_main = "white"
    await db_session.flush()
    owned.add(str(shoes.id))

    app = _build_app(db_session)
    async with _client(app) as client:
        created = await client.post(
            "/v1/packing-lists",
            headers={"X-User-Id": str(user_id)},
            json={
                "destination": "Tokyo",
                "days": 2,
                "scenes": ["business", "casual"],
                "weather_hint": "cold and rainy",
            },
        )
        assert created.status_code == 200
        body = created.json()
        plan = body["plan"]
        assert plan is not None
        # Per-day plan cycles the two occasions and references only owned items.
        assert [d["occasion"] for d in plan["daily_outfits"]] == ["business", "casual"]
        referenced = {iid for d in plan["daily_outfits"] for iid in d["item_ids"]}
        assert referenced and referenced <= owned
        assert "rain" in (plan["weather_summary"] or "")
        # business needs outerwear (absent) + rain/light-shoes → missing warnings.
        categories = {m["category"] for m in plan["missing_items"]}
        assert "outerwear" in categories
        assert "waterproof shoes" in categories
        assert plan["packing_items"]

        # Persisted: GET returns the same plan snapshot.
        pl_id = body["packing_list_id"]
        fetched = await client.get(
            f"/v1/packing-lists/{pl_id}", headers={"X-User-Id": str(user_id)}
        )
    assert fetched.status_code == 200
    assert fetched.json()["plan"]["summary"] == plan["summary"]


async def test_planner_off_leaves_plan_null(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_flag(monkeypatch)  # packing list on, planner off
    user_id = uuid.uuid4()
    await _ensure_user(db_session, user_id)
    await _seed_item(db_session, user_id, "top")
    app = _build_app(db_session)
    async with _client(app) as client:
        resp = await client.post(
            "/v1/packing-lists",
            headers={"X-User-Id": str(user_id)},
            json={"days": 1, "scenes": ["casual"]},
        )
    assert resp.status_code == 200
    assert resp.json()["plan"] is None


async def test_planner_patch_items_clears_stale_plan(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # BE#69 review: editing items must drop the now-inconsistent planner snapshot.
    _enable_planner(monkeypatch)
    user_id = uuid.uuid4()
    await _ensure_user(db_session, user_id)
    await _seed_item(db_session, user_id, "top")
    await _seed_item(db_session, user_id, "bottom")
    await _seed_item(db_session, user_id, "shoes")
    app = _build_app(db_session)
    async with _client(app) as client:
        created = await client.post(
            "/v1/packing-lists",
            headers={"X-User-Id": str(user_id)},
            json={"days": 1, "scenes": ["casual"]},
        )
        body = created.json()
        assert body["plan"] is not None  # planner ran
        pl_id = body["packing_list_id"]
        patched = await client.patch(
            f"/v1/packing-lists/{pl_id}",
            headers={"X-User-Id": str(user_id)},
            json={"items": []},
        )
        assert patched.status_code == 200
        assert patched.json()["plan"] is None  # stale plan cleared
        fetched = await client.get(
            f"/v1/packing-lists/{pl_id}", headers={"X-User-Id": str(user_id)}
        )
    assert fetched.json()["plan"] is None


async def test_planner_excludes_recently_worn(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # BE#69 review: the planner path must also honor DB recent-wear history.
    _enable_planner(monkeypatch)
    user_id = uuid.uuid4()
    await _ensure_user(db_session, user_id)
    recent_top = await _seed_item(db_session, user_id, "top")
    fresh_top = await _seed_item(db_session, user_id, "top")
    await _seed_item(db_session, user_id, "bottom")
    await _seed_item(db_session, user_id, "shoes")

    departure = date(2026, 7, 10)
    daily = DailyRecommendation(
        user_id=user_id, recommendation_date=departure, status="ready", generation_source="manual"
    )
    db_session.add(daily)
    await db_session.flush()
    outfit = Outfit(
        daily_recommendation_id=daily.id, user_id=user_id, outfit_type="safe",
        title="Worn", reason_text="x", score_total=0.5, rank_order=1,
        created_at=datetime.now(UTC),
    )
    db_session.add(outfit)
    await db_session.flush()
    db_session.add(
        OutfitItem(
            outfit_id=outfit.id, item_id=recent_top.id, role="top",
            display_order=1, created_at=datetime.now(UTC),
        )
    )
    db_session.add(
        WearHistory(
            user_id=user_id, outfit_id=outfit.id, worn_date=date(2026, 7, 7),
            selected_at=datetime.now(UTC), created_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    app = _build_app(db_session)
    async with _client(app) as client:
        resp = await client.post(
            "/v1/packing-lists",
            headers={"X-User-Id": str(user_id)},
            json={"days": 2, "scenes": ["casual"], "departure_date": "2026-07-10"},
        )
    assert resp.status_code == 200
    plan = resp.json()["plan"]
    referenced = {iid for d in plan["daily_outfits"] for iid in d["item_ids"]}
    assert str(recent_top.id) not in referenced  # recently worn, excluded
    assert str(fresh_top.id) in referenced  # the un-worn top is used instead


async def test_wear_after_departure_not_treated_as_recent(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # BE#69 review: a wear-history row dated AFTER the trip anchor must not drop
    # the item from candidates (the window is bounded at the anchor).
    _enable_flag(monkeypatch)  # legacy path; same shared recent-wear query
    user_id = uuid.uuid4()
    await _ensure_user(db_session, user_id)
    top = await _seed_item(db_session, user_id, "top")
    await _seed_item(db_session, user_id, "bottom")
    await _seed_item(db_session, user_id, "shoes")

    departure = date(2026, 7, 10)
    daily = DailyRecommendation(
        user_id=user_id, recommendation_date=departure, status="ready", generation_source="manual"
    )
    db_session.add(daily)
    await db_session.flush()
    outfit = Outfit(
        daily_recommendation_id=daily.id, user_id=user_id, outfit_type="safe",
        title="Future wear", reason_text="x", score_total=0.5, rank_order=1,
        created_at=datetime.now(UTC),
    )
    db_session.add(outfit)
    await db_session.flush()
    db_session.add(
        OutfitItem(
            outfit_id=outfit.id, item_id=top.id, role="top",
            display_order=1, created_at=datetime.now(UTC),
        )
    )
    db_session.add(
        WearHistory(
            user_id=user_id, outfit_id=outfit.id, worn_date=date(2026, 7, 12),  # AFTER departure
            selected_at=datetime.now(UTC), created_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    app = _build_app(db_session)
    async with _client(app) as client:
        resp = await client.post(
            "/v1/packing-lists",
            headers={"X-User-Id": str(user_id)},
            json={"days": 1, "scenes": ["casual"], "departure_date": "2026-07-10"},
        )
    assert resp.status_code == 200
    picked = {item["item_id"] for item in resp.json()["items"]}
    assert str(top.id) in picked  # future-dated wear did NOT exclude it
