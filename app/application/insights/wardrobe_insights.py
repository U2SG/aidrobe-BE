"""Wardrobe insights query functions — M12."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.persistence.models.recommendation import WearHistory
from app.infrastructure.persistence.models.wardrobe import WardrobeItem


async def get_summary(user_id: uuid.UUID, session: AsyncSession) -> dict[str, Any]:
    cutoff = datetime.now(tz=UTC) - timedelta(days=60)

    row = await session.execute(
        select(
            func.count().label("total"),
            func.sum(case((WardrobeItem.wear_count > 0, 1), else_=0)).label("active_count"),
            func.sum(
                case(
                    (
                        (WardrobeItem.wear_count == 0) & (WardrobeItem.created_at < cutoff),
                        1,
                    ),
                    else_=0,
                )
            ).label("idle_count"),
        ).where(
            WardrobeItem.user_id == user_id,
            WardrobeItem.deleted_at.is_(None),
        )
    )
    r = row.one()
    total = r.total or 0
    active_count = r.active_count or 0
    idle_count = r.idle_count or 0

    if total == 0:
        return {
            "total_items": 0,
            "active_rate": 0.0,
            "idle_count": 0,
            "idle_threshold_days": 60,
            "most_worn_category": None,
            "favorite_color": None,
        }

    cat_row = await session.execute(
        select(WardrobeItem.category)
        .where(
            WardrobeItem.user_id == user_id,
            WardrobeItem.deleted_at.is_(None),
            WardrobeItem.wear_count > 0,
        )
        .group_by(WardrobeItem.category)
        .order_by(func.count().desc())
        .limit(1)
    )
    most_worn_category = cat_row.scalar_one_or_none()

    color_row = await session.execute(
        select(WardrobeItem.color_main)
        .where(
            WardrobeItem.user_id == user_id,
            WardrobeItem.deleted_at.is_(None),
            WardrobeItem.wear_count > 0,
        )
        .group_by(WardrobeItem.color_main)
        .order_by(func.count().desc())
        .limit(1)
    )
    favorite_color = color_row.scalar_one_or_none()

    return {
        "total_items": total,
        "active_rate": round(active_count / total, 4),
        "idle_count": idle_count,
        "idle_threshold_days": 60,
        "most_worn_category": most_worn_category,
        "favorite_color": favorite_color,
    }


async def get_top_items(user_id: uuid.UUID, session: AsyncSession, limit: int = 10) -> list[Any]:
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
) -> list[Any]:
    cutoff = datetime.now(tz=UTC) - timedelta(days=threshold_days)
    result = await session.execute(
        select(WardrobeItem)
        .where(
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


async def get_style_trend(user_id: uuid.UUID, session: AsyncSession) -> list[Any]:
    """Aggregate style_tags by wear month via WearHistory → OutfitItem → WardrobeItem."""
    from collections import Counter

    from app.infrastructure.persistence.models.recommendation import OutfitItem

    result = await session.execute(
        select(
            func.to_char(WearHistory.worn_date, "YYYY-MM").label("year_month"),
            WardrobeItem.style_tags,
        )
        .join(OutfitItem, OutfitItem.outfit_id == WearHistory.outfit_id)
        .join(WardrobeItem, WardrobeItem.id == OutfitItem.item_id)
        .where(
            WearHistory.user_id == user_id,
            WardrobeItem.style_tags.isnot(None),
        )
        .order_by(func.to_char(WearHistory.worn_date, "YYYY-MM"))
    )
    rows = result.all()

    monthly: dict[str, Counter[str]] = {}
    for year_month, style_tags in rows:
        tags = style_tags if isinstance(style_tags, list) else []
        if year_month not in monthly:
            monthly[year_month] = Counter()
        monthly[year_month].update(tags)

    return [
        {"year_month": ym, "tags": dict(counter)}
        for ym, counter in sorted(monthly.items())
    ]


async def get_cost_per_wear(user_id: uuid.UUID, session: AsyncSession) -> list[Any]:
    result = await session.execute(
        select(WardrobeItem)
        .where(
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
