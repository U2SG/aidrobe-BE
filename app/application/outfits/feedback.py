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
        self, outfit_id: uuid.UUID, user_id: uuid.UUID, feedback_type: str
    ) -> OutfitFeedback:
        if feedback_type not in {FeedbackType.LIKE.value, FeedbackType.DISLIKE.value}:
            raise FriendlyException(
                error_codes.VALIDATION_ERROR, "Invalid feedback type", status_code=422
            )
        outfit = await self.repository.get_outfit_for_user(outfit_id, user_id)
        if outfit is None:
            raise FriendlyException(error_codes.NOT_FOUND, "Outfit not found", status_code=404)
        return await self.repository.add_feedback(
            OutfitFeedback(
                user_id=user_id,
                outfit_id=outfit_id,
                feedback_type=feedback_type,
                created_at=datetime.now(UTC),
            )
        )
