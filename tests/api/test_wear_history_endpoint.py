"""Tests for A-9: GET /v1/wardrobe-items/{item_id}/wear-history endpoint."""

import uuid
from datetime import UTC, date, datetime

from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import Category, ItemStatus
from app.infrastructure.persistence.models.recommendation import (
    DailyRecommendation,
    Outfit,
    OutfitItem,
    WearHistory,
)
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from tests.api.test_wardrobe import build_wardrobe_app, create_item


def _now() -> datetime:
    return datetime.now(tz=UTC)


async def _seed_recommendation(
    session: AsyncSession,
    user_id: uuid.UUID,
    rec_date: date,
) -> uuid.UUID:
    rec = DailyRecommendation(
        user_id=user_id,
        recommendation_date=rec_date,
        status="ready",
        generation_source="test",
        revision=1,
        is_current=True,
        created_at=_now(),
        updated_at=_now(),
    )
    session.add(rec)
    await session.flush()
    return rec.id


async def _seed_outfit_with_item(
    session: AsyncSession,
    user_id: uuid.UUID,
    item_id: uuid.UUID,
    worn_date: date,
    rec_id: uuid.UUID | None = None,
) -> uuid.UUID:
    if rec_id is None:
        rec_id = await _seed_recommendation(session, user_id, worn_date)
    outfit = Outfit(
        daily_recommendation_id=rec_id,
        user_id=user_id,
        outfit_type="casual",
        title="Test outfit",
        reason_text="test",
        score_total=1.0,
        rank_order=1,
        time_sensitivity="standard",
        created_at=_now(),
    )
    session.add(outfit)
    await session.flush()
    session.add(OutfitItem(
        outfit_id=outfit.id,
        item_id=item_id,
        role="top",
        display_order=1,
        created_at=_now(),
    ))
    session.add(WearHistory(
        user_id=user_id,
        outfit_id=outfit.id,
        worn_date=worn_date,
        selected_at=_now(),
        created_at=_now(),
    ))
    await session.flush()
    return outfit.id

async def test_wear_history_empty(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    app = build_wardrobe_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        item_id = await create_item(client, user_id)
        resp = await client.get(
            f"/v1/wardrobe-items/{item_id}/wear-history",
            headers={"X-User-Id": str(user_id)},
        )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["item_id"] == item_id
    assert data["days"] == 60
    assert data["daily_counts"] == {}
    assert data["entries"] == []


async def test_wear_history_multi_wear_same_day(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    db_session.add(User(id=user_id))
    await db_session.flush()
    item = WardrobeItem(
        user_id=user_id,
        title="Shirt",
        category=Category.TOP.value,
        color_main="white",
        status=ItemStatus.WEARABLE.value,
    )
    db_session.add(item)
    await db_session.flush()
    worn_day = date(2026, 5, 1)
    rec_id = await _seed_recommendation(db_session, user_id, worn_day)
    await _seed_outfit_with_item(db_session, user_id, item.id, worn_day, rec_id=rec_id)
    await _seed_outfit_with_item(db_session, user_id, item.id, worn_day, rec_id=rec_id)
    app = build_wardrobe_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get(
            f"/v1/wardrobe-items/{item.id}/wear-history?days=60",
            headers={"X-User-Id": str(user_id)},
        )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["daily_counts"]["2026-05-01"] == 2
    assert len(data["entries"]) == 2


async def test_wear_history_respects_days_param(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    db_session.add(User(id=user_id))
    await db_session.flush()
    item = WardrobeItem(
        user_id=user_id,
        title="Old jacket",
        category=Category.TOP.value,
        color_main="grey",
        status=ItemStatus.WEARABLE.value,
    )
    db_session.add(item)
    await db_session.flush()
    old_date = date(2026, 1, 1)
    await _seed_outfit_with_item(db_session, user_id, item.id, old_date)
    app = build_wardrobe_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp_60 = await client.get(
            f"/v1/wardrobe-items/{item.id}/wear-history?days=60",
            headers={"X-User-Id": str(user_id)},
        )
        resp_200 = await client.get(
            f"/v1/wardrobe-items/{item.id}/wear-history?days=200",
            headers={"X-User-Id": str(user_id)},
        )
    assert resp_60.status_code == 200
    assert resp_60.json()["data"]["daily_counts"] == {}
    assert resp_60.json()["data"]["entries"] == []
    assert resp_200.status_code == 200
    assert resp_200.json()["data"]["daily_counts"].get("2026-01-01") == 1
    assert len(resp_200.json()["data"]["entries"]) == 1


async def test_wear_history_404_for_other_user_item(db_session: AsyncSession) -> None:
    owner_id = uuid.uuid4()
    other_id = uuid.uuid4()
    app = build_wardrobe_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        item_id = await create_item(client, owner_id)
        resp = await client.get(
            f"/v1/wardrobe-items/{item_id}/wear-history",
            headers={"X-User-Id": str(other_id)},
        )
    assert resp.status_code == 404
    assert resp.json()["code"] == 4041
