from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.recommendations.background import (
    RECOMMENDATION_RECOMPUTE_JOB_TYPE,
    RecommendationBackgroundService,
    schedule_today_recompute,
)
from app.common.enums import Category, ItemStatus
from app.common.local_time import local_datetime, utc_now
from app.config.settings import get_settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.recommendation import (
    DailyRecommendation,
    WearHistory,
)
from app.infrastructure.persistence.models.user import User, UserSettings
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)
from app.jobs.recommendation_jobs import cron_departure_pre_generate, cron_nightly_fallback


def _default_local_today() -> date:
    return local_datetime(utc_now(), None, get_settings().DEFAULT_TIMEZONE).date()


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

    # A successor queued while the first mutation job was running must still
    # execute even if the first job committed later than the successor request.
    daily = await SqlAlchemyRecommendationRepository(db_session).get_daily(
        user.id, _default_local_today()
    )
    assert daily is not None
    assert daily.updated_at is not None
    assert await service.recompute_today(
        user.id,
        force=True,
        requested_at=daily.updated_at - timedelta(seconds=1),
    ) is True


async def test_schedule_recompute_only_enqueues_and_debounces(
    db_session: AsyncSession,
    monkeypatch,
) -> None:
    user = User()
    db_session.add(user)
    await db_session.flush()
    repository = SqlAlchemyRecommendationRepository(db_session)
    fixed_now = datetime(2026, 5, 1, 8, 0, tzinfo=UTC)
    monkeypatch.setattr(
        "app.application.recommendations.background.utc_now", lambda: fixed_now
    )

    first = await schedule_today_recompute(user.id, repository)
    second = await schedule_today_recompute(user.id, repository)

    jobs = list(
        await db_session.scalars(
            select(AsyncJob).where(
                AsyncJob.job_type == RECOMMENDATION_RECOMPUTE_JOB_TYPE
            )
        )
    )
    assert first is True
    assert second is False
    assert len(jobs) == 1
    assert jobs[0].status == "queued"
    assert await repository.get_daily(user.id, date(2026, 5, 1)) is None


async def test_replace_daily_preserves_refresh_newer_than_background_request(
    db_session: AsyncSession,
) -> None:
    user = User()
    db_session.add(user)
    await db_session.flush()
    target_date = _default_local_today()
    refresh = DailyRecommendation(
        user_id=user.id,
        recommendation_date=target_date,
        status="ready",
        generation_source="refresh",
    )
    db_session.add(refresh)
    await db_session.flush()
    assert refresh.updated_at is not None

    replacement = DailyRecommendation(
        user_id=user.id,
        recommendation_date=target_date,
        status="insufficient_items",
        generation_source="mutation",
    )
    persisted, should_write_outfits = await SqlAlchemyRecommendationRepository(
        db_session
    ).replace_daily(
        replacement,
        preserve_refresh_after=refresh.updated_at - timedelta(seconds=1),
    )

    assert should_write_outfits is False
    assert persisted.id == refresh.id
    assert persisted.generation_source == "refresh"


async def test_nightly_fallback_selects_users_without_departure_time(
    db_session: AsyncSession,
) -> None:
    user = User()
    db_session.add(user)
    await db_session.flush()
    db_session.add(UserSettings(user_id=user.id, timezone="Asia/Singapore"))
    await db_session.flush()

    run_at = datetime(2026, 4, 30, 19, 5, tzinfo=UTC)
    count = await cron_nightly_fallback(db_session, now=run_at)
    repeated_count = await cron_nightly_fallback(db_session, now=run_at)

    assert count == 1
    assert repeated_count == 0
    assert await SqlAlchemyRecommendationRepository(db_session).get_daily(
        user.id, date(2026, 5, 1)
    ) is not None


async def test_due_departure_users_returns_configured_users(db_session: AsyncSession) -> None:
    user = User()
    later_user = User()
    db_session.add(user)
    db_session.add(later_user)
    await db_session.flush()
    db_session.add(
        UserSettings(
            user_id=user.id,
            departure_time=time(8, 30),
            timezone="Asia/Singapore",
        )
    )
    db_session.add(
        UserSettings(
            user_id=later_user.id,
            departure_time=time(11, 0),
            timezone="Asia/Singapore",
        )
    )
    await db_session.flush()

    service = RecommendationBackgroundService(SqlAlchemyRecommendationRepository(db_session))

    due_users = await service.due_departure_users(
        datetime(2026, 4, 30, 23, 30, tzinfo=UTC)
    )

    assert user.id in due_users
    assert later_user.id not in due_users


async def test_departure_job_generates_once_for_user_local_date(
    db_session: AsyncSession,
) -> None:
    user = User()
    db_session.add(user)
    await db_session.flush()
    db_session.add(
        UserSettings(
            user_id=user.id,
            departure_time=time(8, 30),
            timezone="Asia/Singapore",
        )
    )
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

    run_at = datetime(2026, 5, 1, 23, 30, tzinfo=UTC)
    first_count = await cron_departure_pre_generate(db_session, now=run_at)
    repeated_count = await cron_departure_pre_generate(db_session, now=run_at)

    assert first_count == 1
    assert repeated_count == 0
    assert await SqlAlchemyRecommendationRepository(db_session).get_daily(
        user.id, date(2026, 5, 2)
    ) is not None


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
    daily = await repository.get_daily(user.id, _default_local_today())

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
    local_today = _default_local_today()
    daily = await repository.get_daily(user.id, local_today)
    assert daily is not None
    outfit = daily.outfits[0]
    db_session.add(
        WearHistory(
            user_id=user.id,
            outfit_id=outfit.id,
            selected_at=datetime.now(UTC),
            worn_date=local_today,
            created_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    assert await service.recompute_today(user.id, force=True) is False
    assert len(list(await db_session.scalars(select(WearHistory)))) == 1
