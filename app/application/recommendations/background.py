import uuid
from datetime import UTC, datetime, timedelta

import structlog

from app.application.recommendations.generator import RecommendationGenerator
from app.bootstrap.container import weather_service
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)
from app.infrastructure.persistence.repositories.style_repo import SqlAlchemyStyleRepository

logger = structlog.get_logger(__name__)


async def schedule_today_recompute(
    user_id: uuid.UUID,
    repository: SqlAlchemyRecommendationRepository | None = None,
    style_repository: SqlAlchemyStyleRepository | None = None,
) -> bool:
    if repository is None:
        logger.warning("recommendation_recompute_skipped", reason="missing_repository", user_id=str(user_id))
        return False
    return await RecommendationBackgroundService(repository, style_repository).recompute_today(
        user_id, force=True
    )


class RecommendationBackgroundService:
    def __init__(
        self,
        repository: SqlAlchemyRecommendationRepository,
        style_repository: SqlAlchemyStyleRepository | None = None,
    ) -> None:
        self.repository = repository
        self.style_repository = style_repository

    async def recompute_today(self, user_id: uuid.UUID, force: bool = False) -> bool:
        today = datetime.now(UTC).date()
        existing = await self.repository.get_daily(user_id, today)
        if existing and await self.repository.daily_has_engagement(existing.id):
            logger.info("recommendation_recompute_skipped", reason="daily_has_engagement", user_id=str(user_id))
            return False
        if not force and existing and existing.updated_at:
            updated_at = existing.updated_at
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=UTC)
            if datetime.now(UTC) - updated_at < timedelta(seconds=30):
                return False
        await RecommendationGenerator(
            self.repository, self.style_repository, weather_service=weather_service()
        ).generate_for_user(user_id, today, "mutation")
        return True

    async def users_without_departure_time(self) -> list[uuid.UUID]:
        return [user.id for user in await self.repository.users_without_departure_time()]

    async def due_departure_users(self, now: datetime) -> list[uuid.UUID]:
        users = await self.repository.users_due_for_departure_generation(now)
        return [user.id for user in users]
