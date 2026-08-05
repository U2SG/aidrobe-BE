import uuid
from datetime import date

import structlog

from app.common.enums import RecommendationStatus
from app.infrastructure.persistence.models.recommendation import DailyRecommendation
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)

logger = structlog.get_logger(__name__)


class GetTodayRecommendation:
    def __init__(self, repository: SqlAlchemyRecommendationRepository) -> None:
        self.repository = repository

    async def execute(self, user_id: uuid.UUID, today: date) -> DailyRecommendation:
        daily = await self.repository.get_daily(user_id, today)
        if daily is not None:
            if daily.status == RecommendationStatus.READY.value and not daily.outfits:
                # Issue #100 — /today intermittently reads back a ready daily
                # with zero outfits in CI. Log the row identity plus the
                # is_current invariant (must be exactly 1 per user/date) at
                # read time so the next CI flake pins down which side broke.
                current_count = await self.repository.count_current_dailies(user_id, today)
                logger.warning(
                    "today_ready_daily_has_no_outfits",
                    user_id=str(user_id),
                    recommendation_id=str(daily.id),
                    revision=daily.revision,
                    is_current_count=current_count,
                )
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
