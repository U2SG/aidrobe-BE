"""Tests for B1: wear_count increment on WearOutfit; A-5: last_worn_at + idempotency."""

import uuid
from datetime import date

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.persistence.models.recommendation import WearHistory
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from tests.api.test_outfit_actions import build_outfit_app, get_first_outfit_id
from tests.api.test_recommendations_today import seed_ready_recommendation


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


async def test_wear_double_post_writes_single_history_row(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    await seed_ready_recommendation(db_session, user_id)
    app = build_outfit_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        outfit_id = await get_first_outfit_id(client, user_id)
        payload = {"worn_date": "2026-05-04"}
        r1 = await client.post(
            f"/v1/recommendations/outfits/{outfit_id}/wear",
            headers={"X-User-Id": str(user_id)},
            json=payload,
        )
        r2 = await client.post(
            f"/v1/recommendations/outfits/{outfit_id}/wear",
            headers={"X-User-Id": str(user_id)},
            json=payload,
        )

    assert r1.status_code == 200 and r2.status_code == 200
    count = await db_session.scalar(
        select(func.count(WearHistory.id)).where(
            WearHistory.user_id == user_id,
            WearHistory.outfit_id == uuid.UUID(outfit_id),
            WearHistory.worn_date == date(2026, 5, 4),
        )
    )
    assert count == 1


async def test_wear_updates_last_worn_at_forward_only(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    await seed_ready_recommendation(db_session, user_id)
    app = build_outfit_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        outfit_id = await get_first_outfit_id(client, user_id)
        await client.post(
            f"/v1/recommendations/outfits/{outfit_id}/wear",
            headers={"X-User-Id": str(user_id)},
            json={"worn_date": "2026-05-10"},
        )

    items_after_first = list(
        (
            await db_session.execute(select(WardrobeItem).where(WardrobeItem.user_id == user_id))
        ).scalars()
    )
    worn = [i for i in items_after_first if i.wear_count > 0]
    assert worn and all(i.last_worn_at == date(2026, 5, 10) for i in worn)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        await client.post(
            f"/v1/recommendations/outfits/{outfit_id}/wear",
            headers={"X-User-Id": str(user_id)},
            json={"worn_date": "2026-04-01"},
        )

    items_after_older = list(
        (
            await db_session.execute(select(WardrobeItem).where(WardrobeItem.user_id == user_id))
        ).scalars()
    )
    worn = [i for i in items_after_older if i.wear_count > 0]
    assert worn and all(i.last_worn_at == date(2026, 5, 10) for i in worn)


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
