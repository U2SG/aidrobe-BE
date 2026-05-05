from datetime import UTC, datetime, time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.recommendations.background import RecommendationBackgroundService
from app.common.enums import Category, ItemStatus
from app.infrastructure.persistence.models.recommendation import WearHistory
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
    later_user = User()
    db_session.add(user)
    db_session.add(later_user)
    await db_session.flush()
    db_session.add(UserSettings(user_id=user.id, departure_time=time(8, 30)))
    db_session.add(UserSettings(user_id=later_user.id, departure_time=time(11, 0)))
    await db_session.flush()

    service = RecommendationBackgroundService(SqlAlchemyRecommendationRepository(db_session))

    due_users = await service.due_departure_users(datetime(2026, 5, 1, 7, 30, tzinfo=UTC))

    assert user.id in due_users
    assert later_user.id not in due_users


async def test_schedule_recompute_force_regenerates_after_rapid_mutations(
    db_session: AsyncSession,
) -> None:
    user = User()
    db_session.add(user)
    await db_session.flush()
    repository = SqlAlchemyRecommendationRepository(db_session)
    service = RecommendationBackgroundService(repository)

    db_session.add(
        WardrobeItem(
            user_id=user.id,
            title="White shirt",
            category=Category.TOP.value,
            color_main="white",
            status=ItemStatus.WEARABLE.value,
        )
    )
    await db_session.flush()
    assert await service.recompute_today(user.id, force=True) is True

    for title, category, color in [
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

    assert await service.recompute_today(user.id, force=True) is True
    daily = await repository.get_daily(user.id, datetime.now(UTC).date())

    assert daily is not None
    assert daily.status == "ready"


async def test_recompute_skips_daily_with_wear_history(db_session: AsyncSession) -> None:
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

    repository = SqlAlchemyRecommendationRepository(db_session)
    service = RecommendationBackgroundService(repository)
    assert await service.recompute_today(user.id, force=True) is True
    daily = await repository.get_daily(user.id, datetime.now(UTC).date())
    assert daily is not None
    outfit = daily.outfits[0]
    db_session.add(
        WearHistory(
            user_id=user.id,
            outfit_id=outfit.id,
            selected_at=datetime.now(UTC),
            worn_date=datetime.now(UTC).date(),
            created_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    assert await service.recompute_today(user.id, force=True) is False
    assert len(list(await db_session.scalars(select(WearHistory)))) == 1
