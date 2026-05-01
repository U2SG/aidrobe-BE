import uuid
from datetime import UTC, datetime, timedelta

from app.application.recommendations.generator import RecommendationGenerator
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)


async def schedule_today_recompute(user_id: uuid.UUID) -> None:
    # Implemented in Task 13. Kept as an awaitable hook so mutation use cases can call it now.
    return None


class RecommendationBackgroundService:
    def __init__(self, repository: SqlAlchemyRecommendationRepository) -> None:
        self.repository = repository

    async def recompute_today(self, user_id: uuid.UUID) -> bool:
        today = datetime.now(UTC).date()
        existing = await self.repository.get_daily(user_id, today)
        if existing and existing.updated_at:
            updated_at = existing.updated_at
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=UTC)
            if datetime.now(UTC) - updated_at < timedelta(seconds=30):
                return False
        await RecommendationGenerator(self.repository).generate_for_user(user_id, today, "mutation")
        return True

    async def users_without_departure_time(self) -> list[uuid.UUID]:
        return [user.id for user in await self.repository.users_without_departure_time()]

    async def due_departure_users(self, now: datetime) -> list[uuid.UUID]:
        users = await self.repository.users_due_for_departure_generation(now)
        return [user.id for user in users]
