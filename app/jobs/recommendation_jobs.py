from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.fit_profile.build_fit_profile import build_all_fit_profiles
from app.application.recommendations.background import schedule_today_recompute
from app.application.recommendations.generator import RecommendationGenerator
from app.bootstrap.container import llm_service, push_provider, weather_service
from app.common.enums import RecommendationGenerationSource
from app.common.local_time import local_datetime, utc_now
from app.config.settings import get_settings
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)
from app.infrastructure.persistence.repositories.style_repo import SqlAlchemyStyleRepository

logger = structlog.get_logger(__name__)

# The midday checkpoint fires once per local day: the scheduler ticks every
# 15 minutes, so accepting only the first quarter of the 11:00 hour yields
# exactly one trigger window (a scheduler restart across the window skips the
# day — acceptable for a backstop).
_MIDDAY_HOUR = 11


async def cron_midday_regenerate(session: AsyncSession, now: datetime | None = None) -> int:
    """BE#126 — 11:00-local checkpoint: users who haven't opened the app today
    get their daily regenerated with the current day-outlook weather, so the
    first open of the afternoon isn't dressed for 3 a.m. conditions. Reuses
    the recompute pipeline, which already guards engaged / refresh-superseded
    dailies and invalidates the /today cache. Evenings deliberately have no
    checkpoint — the tomorrow-preview mode owns them (product decision)."""
    repository = SqlAlchemyRecommendationRepository(session)
    style_repository = SqlAlchemyStyleRepository(session)
    run_at = now or utc_now()
    default_timezone = get_settings().DEFAULT_TIMEZONE
    scheduled_count = 0
    for user in await repository.users_with_settings():
        user_timezone = user.settings.timezone if user.settings else None
        local_now = local_datetime(run_at, user_timezone, default_timezone)
        if local_now.hour != _MIDDAY_HOUR or local_now.minute >= 15:
            continue
        day_start_utc = local_now.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
        if await repository.has_app_open_since(user.id, day_start_utc):
            continue
        if await schedule_today_recompute(
            user.id,
            repository,
            style_repository,
            generation_source=RecommendationGenerationSource.MIDDAY_CHECKPOINT,
        ):
            scheduled_count += 1
    logger.info("midday_regenerate_completed", scheduled_count=scheduled_count)
    return scheduled_count


async def cron_nightly_fallback(session: AsyncSession, now: datetime | None = None) -> int:
    repository = SqlAlchemyRecommendationRepository(session)
    style_repository = SqlAlchemyStyleRepository(session)
    run_at = now or utc_now()
    default_timezone = get_settings().DEFAULT_TIMEZONE
    users = await repository.users_without_departure_time()
    generated_count = 0
    for user in users:
        user_timezone = user.settings.timezone if user.settings else None
        local_now = local_datetime(run_at, user_timezone, default_timezone)
        if local_now.hour != 3:
            continue
        target_date = local_now.date()
        if await repository.get_daily(user.id, target_date) is not None:
            continue
        daily = await RecommendationGenerator(
            repository, style_repository, llm=llm_service(), weather_service=weather_service()
        ).generate_for_user(user.id, target_date, "nightly_fallback")
        generated_count += 1
        if user.settings and user.settings.push_enabled and user.settings.device_token:
            await push_provider().send_recommendation_ready(
                user.settings.device_token, len(daily.__dict__.get("outfits", [])) or 3
            )
    logger.info("nightly_fallback_completed", user_count=generated_count)
    return generated_count


async def cron_departure_pre_generate(session: AsyncSession, now: datetime | None = None) -> int:
    repository = SqlAlchemyRecommendationRepository(session)
    style_repository = SqlAlchemyStyleRepository(session)
    run_at = now or utc_now()
    default_timezone = get_settings().DEFAULT_TIMEZONE
    users = await repository.users_due_for_departure_generation(run_at, default_timezone)
    generated_count = 0
    for user in users:
        user_timezone = user.settings.timezone if user.settings else None
        target_date = local_datetime(run_at, user_timezone, default_timezone).date()
        if await repository.get_daily(user.id, target_date) is not None:
            continue
        daily = await RecommendationGenerator(
            repository, style_repository, llm=llm_service(), weather_service=weather_service()
        ).generate_for_user(user.id, target_date, "departure_pre_generate")
        generated_count += 1
        if user.settings and user.settings.push_enabled and user.settings.device_token:
            await push_provider().send_recommendation_ready(
                user.settings.device_token, len(daily.__dict__.get("outfits", [])) or 3
            )
    logger.info("departure_pre_generate_completed", user_count=generated_count)
    return generated_count


async def cron_fit_profile_aggregation(session: AsyncSession) -> int:
    """Nightly fit profile aggregation — pre-compute per-user fit preferences."""
    user_count = await build_all_fit_profiles(session)
    logger.info("fit_profile_aggregation_completed", user_count=user_count)
    return user_count
