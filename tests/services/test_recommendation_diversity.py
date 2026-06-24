"""BE#39 — recently-recommended history queries used by diversity (DB)."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.persistence.models.recommendation import (
    DailyRecommendation,
    Outfit,
    OutfitItem,
)
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)

_TARGET = date(2026, 6, 24)


async def _item(session: AsyncSession, user_id: uuid.UUID, category: str, color: str) -> uuid.UUID:
    item = WardrobeItem(user_id=user_id, title=f"{color} {category}", category=category, color_main=color)
    session.add(item)
    await session.flush()
    return item.id


async def _seed_recommended_outfit(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    rec_date: date,
    pieces: list[tuple[str, str]],  # (category, color)
    is_current: bool = True,
) -> set[str]:
    daily = DailyRecommendation(
        user_id=user_id,
        recommendation_date=rec_date,
        status="ready",
        generation_source="background",
        is_current=is_current,
    )
    session.add(daily)
    await session.flush()
    outfit = Outfit(
        daily_recommendation_id=daily.id,
        user_id=user_id,
        outfit_type="safe",
        title="x",
        reason_text="x",
        score_total=Decimal("1.0"),
        rank_order=1,
        created_at=datetime.now(UTC),
    )
    session.add(outfit)
    await session.flush()
    item_ids: set[str] = set()
    for order, (category, color) in enumerate(pieces, start=1):
        item_id = await _item(session, user_id, category, color)
        session.add(
            OutfitItem(
                outfit_id=outfit.id,
                item_id=item_id,
                role=category,
                display_order=order,
                created_at=datetime.now(UTC),
            )
        )
        item_ids.add(str(item_id))
    await session.flush()
    return item_ids


async def test_recent_recommended_outfit_item_ids_windows_and_excludes_today(
    db_session: AsyncSession,
) -> None:
    user_id = uuid.uuid4()
    db_session.add(User(id=user_id))
    await db_session.flush()

    in_window = await _seed_recommended_outfit(
        db_session, user_id, rec_date=_TARGET - timedelta(days=2),
        pieces=[("top", "white"), ("bottom", "black")],
    )
    # Out of the 7-day window — must be excluded.
    await _seed_recommended_outfit(
        db_session, user_id, rec_date=_TARGET - timedelta(days=30),
        pieces=[("top", "blue"), ("bottom", "grey")],
    )
    # Today itself — excluded (recommendation_date < target).
    await _seed_recommended_outfit(
        db_session, user_id, rec_date=_TARGET, pieces=[("dress", "red")],
    )

    repo = SqlAlchemyRecommendationRepository(db_session)
    sets = await repo.get_recent_recommended_outfit_item_ids(user_id, _TARGET, days=7)
    assert sets == [in_window]


async def test_recent_recommended_main_item_ids_only_top_or_dress(
    db_session: AsyncSession,
) -> None:
    user_id = uuid.uuid4()
    db_session.add(User(id=user_id))
    await db_session.flush()

    await _seed_recommended_outfit(
        db_session, user_id, rec_date=_TARGET - timedelta(days=1),
        pieces=[("top", "white"), ("bottom", "black"), ("shoes", "brown")],
    )
    repo = SqlAlchemyRecommendationRepository(db_session)
    mains = await repo.recent_recommended_main_item_ids(user_id, _TARGET, days=7)
    # Exactly the top item — bottom/shoes are not hero items.
    items = await repo.get_recent_recommended_outfit_item_ids(user_id, _TARGET, days=7)
    assert len(mains) == 1
    assert mains.issubset(items[0])
