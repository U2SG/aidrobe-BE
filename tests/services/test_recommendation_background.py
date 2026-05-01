from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.recommendations.background import RecommendationBackgroundService
from app.common.enums import Category, ItemStatus
from app.infrastructure.persistence.models.user import User, UserSettings
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)
from app.jobs.recommendation_jobs import cron_nightly_fallback


async def test_recompute_today_generates_once_then_debounces(db_session: AsyncSession) -> None:
    user = User()
    db_session.add(user)
    await db_session.flush()
    for title, category, color in [
        ("White shirt", Category.TOP.value, "white"),
        ("Blue shirt", Category.TOP.value, "blue"),
        ("Black pants", Category.BOTTOM.value, "black"),
    ]:
        db_session.add(
            WardrobeItem(
                user_id=user.id,
                title=title,
                category=category,
                color_main=color,
                status=ItemStatus.WEARABLE.value,
            )
        )
    await db_session.flush()

    service = RecommendationBackgroundService(SqlAlchemyRecommendationRepository(db_session))

    assert await service.recompute_today(user.id) is True
    assert await service.recompute_today(user.id) is False


async def test_nightly_fallback_selects_users_without_departure_time(
    db_session: AsyncSession,
) -> None:
    user = User()
    db_session.add(user)
    await db_session.flush()

    count = await cron_nightly_fallback(db_session)

    assert count == 1


async def test_due_departure_users_returns_configured_users(db_session: AsyncSession) -> None:
    user = User()
    db_session.add(user)
    await db_session.flush()
    db_session.add(UserSettings(user_id=user.id, departure_time=datetime.now(UTC).time()))
    await db_session.flush()

    service = RecommendationBackgroundService(SqlAlchemyRecommendationRepository(db_session))

    assert user.id in await service.due_departure_users(datetime.now(UTC))
