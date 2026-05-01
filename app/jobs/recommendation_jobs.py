from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.recommendations.generator import RecommendationGenerator
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)

logger = structlog.get_logger(__name__)


async def cron_nightly_fallback(session: AsyncSession) -> int:
    repository = SqlAlchemyRecommendationRepository(session)
    users = await repository.users_without_departure_time()
    for user in users:
        await RecommendationGenerator(repository).generate_for_user(
            user.id, datetime.now(UTC).date(), "nightly_fallback"
        )
    logger.info("nightly_fallback_completed", user_count=len(users))
    return len(users)


async def cron_departure_pre_generate(session: AsyncSession, now: datetime | None = None) -> int:
    repository = SqlAlchemyRecommendationRepository(session)
    users = await repository.users_due_for_departure_generation(now or datetime.now(UTC))
    for user in users:
        await RecommendationGenerator(repository).generate_for_user(
            user.id, datetime.now(UTC).date(), "departure_pre_generate"
        )
    logger.info("departure_pre_generate_completed", user_count=len(users))
    return len(users)
