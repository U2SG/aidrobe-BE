"""Tests for B1: wear_count increment on WearOutfit."""

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from tests.api.test_outfit_actions import build_outfit_app, get_first_outfit_id
from tests.api.test_recommendations_today import seed_ready_recommendation

from httpx import ASGITransport, AsyncClient


async def test_wear_outfit_increments_wear_count(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    await seed_ready_recommendation(db_session, user_id)
    app = build_outfit_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        outfit_id = await get_first_outfit_id(client, user_id)
        resp = await client.post(
            f"/v1/recommendations/outfits/{outfit_id}/wear",
            headers={"X-User-Id": str(user_id)},
            json={"worn_date": "2026-05-01"},
        )

    assert resp.status_code == 200

    result = await db_session.execute(
        select(WardrobeItem).where(WardrobeItem.user_id == user_id)
    )
    items = list(result.scalars().all())
    worn_items = [i for i in items if i.wear_count > 0]
    assert len(worn_items) > 0, "At least one item should have wear_count incremented"
    assert all(i.wear_count == 1 for i in worn_items)


async def test_wear_outfit_idempotent_does_not_double_increment(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    await seed_ready_recommendation(db_session, user_id)
    app = build_outfit_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        outfit_id = await get_first_outfit_id(client, user_id)
        payload = {"worn_date": "2026-05-02"}
        await client.post(
            f"/v1/recommendations/outfits/{outfit_id}/wear",
            headers={"X-User-Id": str(user_id)},
            json=payload,
        )
        await client.post(
            f"/v1/recommendations/outfits/{outfit_id}/wear",
            headers={"X-User-Id": str(user_id)},
            json=payload,
        )

    result = await db_session.execute(
        select(WardrobeItem).where(WardrobeItem.user_id == user_id)
    )
    items = list(result.scalars().all())
    worn_items = [i for i in items if i.wear_count > 0]
    # Idempotent: same date means second wear is a no-op, count stays at 1
    assert all(i.wear_count == 1 for i in worn_items)


async def test_wear_different_dates_increments_twice(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    await seed_ready_recommendation(db_session, user_id)
    app = build_outfit_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        outfit_id = await get_first_outfit_id(client, user_id)
        await client.post(
            f"/v1/recommendations/outfits/{outfit_id}/wear",
            headers={"X-User-Id": str(user_id)},
            json={"worn_date": "2026-05-01"},
        )
        await client.post(
            f"/v1/recommendations/outfits/{outfit_id}/wear",
            headers={"X-User-Id": str(user_id)},
            json={"worn_date": "2026-05-03"},
        )

    result = await db_session.execute(
        select(WardrobeItem).where(WardrobeItem.user_id == user_id)
    )
    items = list(result.scalars().all())
    worn_items = [i for i in items if i.wear_count > 0]
    assert all(i.wear_count == 2 for i in worn_items)
