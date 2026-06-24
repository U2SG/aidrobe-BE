import uuid
from datetime import UTC, datetime

from app.common import error_codes
from app.common.enums import FeedbackType, RecommendationEventType
from app.common.exceptions import FriendlyException
from app.infrastructure.persistence.models.recommendation import OutfitFeedback, RecommendationEvent
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)

# BE#40 — outfit feedback is also an interaction signal. Polar ratings map to
# liked/disliked events; `neutral` carries no event_type of its own but the raw
# rating is always preserved in event_metadata, so no signal is discarded.
_FEEDBACK_EVENT_MAP = {
    FeedbackType.LOVE.value: RecommendationEventType.LIKED.value,
    FeedbackType.LIKE.value: RecommendationEventType.LIKED.value,
    FeedbackType.DISLIKE.value: RecommendationEventType.DISLIKED.value,
}


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

        # BE#40 — record the interaction as an attributed event (always, no flag:
        # the learning stream must accumulate even before any ranking consumes it).
        event_type = _FEEDBACK_EVENT_MAP.get(feedback_type)
        if event_type is not None:
            await self.repository.add_recommendation_event(
                RecommendationEvent(
                    user_id=user_id,
                    recommendation_id=outfit.daily_recommendation_id,
                    outfit_id=outfit_id,
                    event_type=event_type,
                    source="outfit_feedback",
                    event_metadata={"feedback_type": feedback_type, "reason_tags": reason_tags or []},
                    created_at=datetime.now(UTC),
                )
            )

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
