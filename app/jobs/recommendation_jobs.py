from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.fit_profile.build_fit_profile import build_all_fit_profiles
from app.application.recommendations.generator import RecommendationGenerator
from app.bootstrap.container import llm_service, push_provider, weather_service
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)
from app.infrastructure.persistence.repositories.style_repo import SqlAlchemyStyleRepository

logger = structlog.get_logger(__name__)


async def cron_nightly_fallback(session: AsyncSession) -> int:
    repository = SqlAlchemyRecommendationRepository(session)
    style_repository = SqlAlchemyStyleRepository(session)
    users = await repository.users_without_departure_time()
    for user in users:
        daily = await RecommendationGenerator(
            repository, style_repository, llm=llm_service(), weather_service=weather_service()
        ).generate_for_user(user.id, datetime.now(UTC).date(), "nightly_fallback")
        if user.settings and user.settings.push_enabled and user.settings.device_token:
            await push_provider().send_recommendation_ready(
                user.settings.device_token, len(daily.__dict__.get("outfits", [])) or 3
            )
    logger.info("nightly_fallback_completed", user_count=len(users))
    return len(users)


async def cron_departure_pre_generate(session: AsyncSession, now: datetime | None = None) -> int:
    repository = SqlAlchemyRecommendationRepository(session)
    style_repository = SqlAlchemyStyleRepository(session)
    users = await repository.users_due_for_departure_generation(now or datetime.now(UTC))
    for user in users:
        daily = await RecommendationGenerator(
            repository, style_repository, llm=llm_service(), weather_service=weather_service()
        ).generate_for_user(user.id, datetime.now(UTC).date(), "departure_pre_generate")
        if user.settings and user.settings.push_enabled and user.settings.device_token:
            await push_provider().send_recommendation_ready(
                user.settings.device_token, len(daily.__dict__.get("outfits", [])) or 3
            )
    logger.info("departure_pre_generate_completed", user_count=len(users))
    return len(users)


async def cron_fit_profile_aggregation(session: AsyncSession) -> int:
    """Nightly fit profile aggregation — pre-compute per-user fit preferences."""
    user_count = await build_all_fit_profiles(session)
    logger.info("fit_profile_aggregation_completed", user_count=user_count)
    return user_count
