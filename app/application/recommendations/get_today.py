import uuid
from datetime import date

from app.common.enums import RecommendationStatus
from app.infrastructure.persistence.models.recommendation import DailyRecommendation
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)


class GetTodayRecommendation:
    def __init__(self, repository: SqlAlchemyRecommendationRepository) -> None:
        self.repository = repository

    async def execute(self, user_id: uuid.UUID, today: date) -> DailyRecommendation:
        daily = await self.repository.get_daily(user_id, today)
        if daily is not None:
            return daily

        fallback = await self.repository.get_latest_before(user_id, today)
        if fallback is not None:
            return fallback

        return DailyRecommendation(
            user_id=user_id,
            recommendation_date=today,
            status=RecommendationStatus.INSUFFICIENT_ITEMS.value,
            generation_source="fallback",
            score_breakdown=None,
        )
