"""Wardrobe insights query functions — M12."""

import uuid
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.persistence.models.wardrobe import WardrobeItem


async def get_summary(user_id: uuid.UUID, session: AsyncSession) -> dict:
    result = await session.execute(
        select(WardrobeItem).where(
            WardrobeItem.user_id == user_id,
            WardrobeItem.deleted_at.is_(None),
        )
    )
    items = list(result.scalars().all())
    total = len(items)
    if total == 0:
        return {
            "total_items": 0,
            "active_rate": 0.0,
            "idle_count": 0,
            "idle_threshold_days": 60,
            "most_worn_category": None,
            "favorite_color": None,
        }

    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=60)
    idle_count = sum(1 for i in items if i.wear_count == 0 and i.created_at < cutoff)
    active_count = sum(1 for i in items if i.wear_count > 0)
    active_rate = round(active_count / total, 4) if total > 0 else 0.0

    category_counts: Counter = Counter(i.category for i in items if i.wear_count > 0)
    color_counts: Counter = Counter(i.color_main for i in items if i.wear_count > 0)

    return {
        "total_items": total,
        "active_rate": active_rate,
        "idle_count": idle_count,
        "idle_threshold_days": 60,
        "most_worn_category": category_counts.most_common(1)[0][0] if category_counts else None,
        "favorite_color": color_counts.most_common(1)[0][0] if color_counts else None,
    }


async def get_top_items(user_id: uuid.UUID, session: AsyncSession, limit: int = 10) -> list:
    result = await session.execute(
        select(WardrobeItem)
        .where(
            WardrobeItem.user_id == user_id,
            WardrobeItem.deleted_at.is_(None),
        )
        .order_by(WardrobeItem.wear_count.desc())
        .limit(limit)
    )
    return list(result.scalars().all())


async def get_idle_items(
    user_id: uuid.UUID, session: AsyncSession, threshold_days: int = 60
) -> list:
    cutoff = datetime.now(tz=timezone.utc) - timedelta(days=threshold_days)
    result = await session.execute(
        select(WardrobeItem).where(
            WardrobeItem.user_id == user_id,
            WardrobeItem.deleted_at.is_(None),
            WardrobeItem.wear_count == 0,
            WardrobeItem.created_at < cutoff,
        )
    )
    return list(result.scalars().all())


async def get_color_palette(user_id: uuid.UUID, session: AsyncSession) -> dict[str, int]:
    result = await session.execute(
        select(WardrobeItem.color_main, func.count().label("cnt"))
        .where(
            WardrobeItem.user_id == user_id,
            WardrobeItem.deleted_at.is_(None),
        )
        .group_by(WardrobeItem.color_main)
        .order_by(func.count().desc())
    )
    return {row.color_main: row.cnt for row in result.all()}


async def get_style_trend(user_id: uuid.UUID, session: AsyncSession) -> list:
    result = await session.execute(
        select(WardrobeItem).where(
            WardrobeItem.user_id == user_id,
            WardrobeItem.deleted_at.is_(None),
            WardrobeItem.style_tags.isnot(None),
        )
    )
    items = list(result.scalars().all())

    monthly: dict[str, Counter] = {}
    for item in items:
        ym = item.created_at.strftime("%Y-%m")
        tags = item.style_tags or []
        if isinstance(tags, list):
            if ym not in monthly:
                monthly[ym] = Counter()
            monthly[ym].update(tags)

    return [
        {"year_month": ym, "tags": dict(counter)}
        for ym, counter in sorted(monthly.items())
    ]


async def get_cost_per_wear(user_id: uuid.UUID, session: AsyncSession) -> list:
    result = await session.execute(
        select(WardrobeItem).where(
            WardrobeItem.user_id == user_id,
            WardrobeItem.deleted_at.is_(None),
            WardrobeItem.purchase_price.isnot(None),
        )
    )
    items = list(result.scalars().all())
    rows = []
    for item in items:
        cpw = None
        if item.purchase_price and item.wear_count > 0:
            cpw = round(Decimal(str(item.purchase_price)) / item.wear_count, 2)
        rows.append(
            {
                "item_id": item.id,
                "title": item.title,
                "purchase_price": item.purchase_price,
                "wear_count": item.wear_count,
                "cost_per_wear": cpw,
            }
        )
    return rows
