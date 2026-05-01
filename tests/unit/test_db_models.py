from datetime import UTC, date, datetime, time
from decimal import Decimal

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import Category, ImageType, ItemStatus, OutfitType, RecommendationStatus
from app.infrastructure.persistence.models.recommendation import (
    DailyRecommendation,
    Outfit,
    OutfitItem,
)
from app.infrastructure.persistence.models.user import User, UserSettings
from app.infrastructure.persistence.models.wardrobe import WardrobeItem, WardrobeItemImage


async def test_create_user_settings_wardrobe_and_recommendation(
    db_session: AsyncSession,
) -> None:
    now = datetime.now(UTC)
    user = User()
    db_session.add(user)
    await db_session.flush()

    settings = UserSettings(user_id=user.id, departure_time=time(hour=8, minute=15))
    db_session.add(settings)

    item = WardrobeItem(
        user_id=user.id,
        title="White shirt",
        category=Category.TOP.value,
        color_main="white",
        status=ItemStatus.WEARABLE.value,
        cover_image_url="https://cdn.example/item.jpg",
        original_image_url="https://origin.example/item.jpg",
    )
    db_session.add(item)
    await db_session.flush()

    image = WardrobeItemImage(
        item_id=item.id,
        image_type=ImageType.ORIGINAL.value,
        image_url="https://origin.example/item.jpg",
        created_at=now,
    )
    recommendation = DailyRecommendation(
        user_id=user.id,
        recommendation_date=date(2026, 5, 1),
        status=RecommendationStatus.READY.value,
        generation_source="manual",
        score_breakdown={"color_score": 1.0},
    )
    db_session.add_all([image, recommendation])
    await db_session.flush()

    outfit = Outfit(
        daily_recommendation_id=recommendation.id,
        user_id=user.id,
        outfit_type=OutfitType.SAFE.value,
        title="Safe outfit",
        reason_text="Classic colors for commute",
        score_total=Decimal("1.0000"),
        rank_order=1,
        created_at=now,
    )
    db_session.add(outfit)
    await db_session.flush()

    outfit_item = OutfitItem(
        outfit_id=outfit.id,
        item_id=item.id,
        role=Category.TOP.value,
        display_order=1,
        created_at=now,
    )
    db_session.add(outfit_item)
    await db_session.flush()

    persisted = await db_session.scalar(select(DailyRecommendation))

    assert persisted is not None
    assert persisted.user_id == user.id
    assert persisted.recommendation_date == date(2026, 5, 1)


async def test_daily_recommendation_unique_per_user_date(db_session: AsyncSession) -> None:
    user = User()
    db_session.add(user)
    await db_session.flush()

    first = DailyRecommendation(
        user_id=user.id,
        recommendation_date=date(2026, 5, 1),
        status=RecommendationStatus.READY.value,
        generation_source="manual",
    )
    second = DailyRecommendation(
        user_id=user.id,
        recommendation_date=date(2026, 5, 1),
        status=RecommendationStatus.READY.value,
        generation_source="manual",
    )
    db_session.add_all([first, second])

    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()
