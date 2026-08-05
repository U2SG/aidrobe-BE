from datetime import UTC, date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports.weather import WeatherInfo
from app.application.recommendations.background import (
    RECOMMENDATION_RECOMPUTE_JOB_TYPE,
    RecommendationBackgroundService,
    schedule_today_recompute,
)
from app.application.recommendations.generator import RecommendationGenerator
from app.common.enums import Category, ItemStatus
from app.common.local_time import local_datetime, utc_now
from app.config.settings import get_settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.operation_log import OperationLog
from app.infrastructure.persistence.models.recommendation import (
    DailyRecommendation,
    WearHistory,
)
from app.infrastructure.persistence.models.user import User, UserSettings
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)
from app.jobs.recommendation_jobs import (
    cron_departure_pre_generate,
    cron_midday_regenerate,
    cron_nightly_fallback,
)


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


# ── midday checkpoint (BE#126) ───────────────────────────────────────
async def _seed_sg_user(db_session: AsyncSession) -> User:
    user = User()
    db_session.add(user)
    await db_session.flush()
    db_session.add(UserSettings(user_id=user.id, timezone="Asia/Singapore"))
    await db_session.flush()
    return user


async def _recompute_jobs(db_session: AsyncSession) -> list[AsyncJob]:
    rows = await db_session.scalars(
        select(AsyncJob).where(AsyncJob.job_type == RECOMMENDATION_RECOMPUTE_JOB_TYPE)
    )
    return list(rows)


async def test_midday_regenerate_schedules_for_unopened_users(db_session: AsyncSession) -> None:
    user = await _seed_sg_user(db_session)
    # 03:05 UTC == 11:05 Asia/Singapore — inside the single trigger window.
    run_at = datetime(2026, 7, 25, 3, 5, tzinfo=UTC)

    count = await cron_midday_regenerate(db_session, now=run_at)

    assert count == 1
    jobs = await _recompute_jobs(db_session)
    assert len(jobs) == 1 and jobs[0].user_id == user.id
    assert jobs[0].payload is not None
    assert jobs[0].payload["generation_source"] == "midday_checkpoint"


async def test_midday_regenerate_skips_users_who_opened_today(db_session: AsyncSession) -> None:
    user = await _seed_sg_user(db_session)
    db_session.add(OperationLog(user_id=user.id, event="app_open"))
    await db_session.flush()
    run_at = datetime(2026, 7, 25, 3, 5, tzinfo=UTC)

    count = await cron_midday_regenerate(db_session, now=run_at)

    assert count == 0
    assert await _recompute_jobs(db_session) == []


async def test_midday_regenerate_fires_only_in_the_first_quarter(db_session: AsyncSession) -> None:
    await _seed_sg_user(db_session)
    # 11:20 local — outside the first-quarter window; and 10:05 local — wrong hour.
    for run_at in (
        datetime(2026, 7, 25, 3, 20, tzinfo=UTC),
        datetime(2026, 7, 25, 2, 5, tzinfo=UTC),
    ):
        assert await cron_midday_regenerate(db_session, now=run_at) == 0
    assert await _recompute_jobs(db_session) == []


# ── day-outlook weather for today (BE#126) ───────────────────────────
class _SplitWeather:
    """Distinguishable forecast vs instantaneous responses."""

    def __init__(self, forecast_available: bool) -> None:
        self.forecast_available = forecast_available

    async def get_weather(self, city: str) -> WeatherInfo:
        return WeatherInfo(temperature_c=27.0, feels_like_c=27.0, rain_probability=0.0, condition="now")

    async def get_today(self, location: str) -> WeatherInfo:
        return await self.get_weather(location)

    async def get_forecast(self, location: str, target_date: date) -> WeatherInfo | None:
        if not self.forecast_available:
            return None
        return WeatherInfo(temperature_c=35.0, feels_like_c=37.0, rain_probability=0.8, condition="outlook")


async def test_generation_weather_prefers_day_outlook_with_now_fallback(
    db_session: AsyncSession,
) -> None:
    user = User()
    db_session.add(user)
    await db_session.flush()
    db_session.add(UserSettings(user_id=user.id, timezone="Asia/Singapore", location="Shenzhen"))
    await db_session.flush()
    repository = SqlAlchemyRecommendationRepository(db_session)

    outlook = await RecommendationGenerator(
        repository, weather_service=_SplitWeather(forecast_available=True)
    )._load_weather(user.id)
    assert outlook is not None and outlook.condition == "outlook"

    fallback = await RecommendationGenerator(
        repository, weather_service=_SplitWeather(forecast_available=False)
    )._load_weather(user.id)
    assert fallback is not None and fallback.condition == "now"


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
