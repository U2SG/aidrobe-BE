from datetime import datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.fit_profile.build_fit_profile import build_all_fit_profiles
from app.application.recommendations.generator import RecommendationGenerator
from app.bootstrap.container import llm_service, push_provider, weather_service
from app.common.local_time import local_datetime, utc_now
from app.config.settings import get_settings
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)
from app.infrastructure.persistence.repositories.style_repo import SqlAlchemyStyleRepository

logger = structlog.get_logger(__name__)


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
