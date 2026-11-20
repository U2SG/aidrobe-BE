import uuid
from datetime import UTC, date, datetime

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import RecommendationEventType
from app.infrastructure.persistence.models.recommendation import RecommendationEvent
from app.infrastructure.persistence.models.wardrobe import WardrobeItem


async def record_manual_wear(session: AsyncSession, user_id: uuid.UUID, item_id: uuid.UUID, worn_date: date) -> bool:
    """Record a standalone wear of an item the caller already authorized.

    Idempotent per (item, worn_date), mirroring the outfit wear path — a double
    tap must not double-count. Returns False when that day was already recorded.
    """
    duplicate = await session.scalar(
        select(RecommendationEvent.id).where(
            RecommendationEvent.user_id == user_id,
            RecommendationEvent.item_id == item_id,
            RecommendationEvent.event_type == RecommendationEventType.WORN.value,
            RecommendationEvent.source == "manual",
            RecommendationEvent.event_metadata["worn_date"].astext == worn_date.isoformat(),
        )
    )
    if duplicate is not None:
        return False
    session.add(
        RecommendationEvent(
            user_id=user_id,
            item_id=item_id,
            event_type=RecommendationEventType.WORN.value,
            source="manual",
            event_metadata={"worn_date": worn_date.isoformat()},
            created_at=datetime.now(UTC),
        )
    )
    # Atomic in SQL (same pattern as the outfit wear path): concurrent wears
    # must not lose increments, and last_worn_at only moves forward.
    await session.execute(
        update(WardrobeItem)
        .where(WardrobeItem.id == item_id)
        .values(
            wear_count=WardrobeItem.wear_count + 1,
            last_worn_at=func.greatest(func.coalesce(WardrobeItem.last_worn_at, worn_date), worn_date),
        )
    )
    await session.flush()
    return True
