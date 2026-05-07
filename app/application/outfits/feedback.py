import uuid
from datetime import UTC, datetime

from app.common import error_codes
from app.common.enums import FeedbackType
from app.common.exceptions import FriendlyException
from app.infrastructure.persistence.models.recommendation import OutfitFeedback
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)


class FeedbackOutfit:
    def __init__(self, repository: SqlAlchemyRecommendationRepository) -> None:
        self.repository = repository

    async def execute(
        self,
        outfit_id: uuid.UUID,
        user_id: uuid.UUID,
        feedback_type: str,
        reason_tags: list[str] | None = None,
    ) -> OutfitFeedback:
        if feedback_type not in {feedback.value for feedback in FeedbackType}:
            raise FriendlyException(
                error_codes.VALIDATION_ERROR, "Invalid feedback type", status_code=422
            )
        outfit = await self.repository.get_outfit_for_user(outfit_id, user_id)
        if outfit is None:
            raise FriendlyException(error_codes.NOT_FOUND, "Outfit not found", status_code=404)
        existing = await self.repository.get_feedback_for_user(outfit_id, user_id)
        if existing is not None:
            existing.feedback_type = feedback_type
            existing.reason_tags = {"tags": reason_tags or []}
            return existing
        return await self.repository.add_feedback(
            OutfitFeedback(
                user_id=user_id,
                outfit_id=outfit_id,
                feedback_type=feedback_type,
                reason_tags={"tags": reason_tags or []},
                created_at=datetime.now(UTC),
            )
        )
