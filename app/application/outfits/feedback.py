import uuid
from datetime import UTC, datetime, timedelta

import structlog

from app.application.preferences.memory import (
    SOURCE_FEEDBACK,
    PreferenceMemoryError,
    record_preference,
)
from app.application.recommendations.feedback_learning import (
    DEFAULT_SUPPRESSION_DAYS,
    derive_learning_signals,
)
from app.common import error_codes
from app.common.enums import FeedbackType, RecommendationEventType
from app.common.exceptions import FriendlyException
from app.config.settings import get_settings
from app.infrastructure.persistence.models.recommendation import OutfitFeedback, RecommendationEvent
from app.infrastructure.persistence.repositories.preference_memory_repo import (
    SqlAlchemyPreferenceMemoryRepository,
)
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)

logger = structlog.get_logger(__name__)

# BE#40 — outfit feedback is also an interaction signal. Every rating emits an
# event (love/like → liked, dislike → disliked, neutral → neutral) so the
# append-only learning stream never loses a neutral the user later overwrites;
# the raw rating is also preserved in event_metadata.
_FEEDBACK_EVENT_MAP = {
    FeedbackType.LOVE.value: RecommendationEventType.LIKED.value,
    FeedbackType.LIKE.value: RecommendationEventType.LIKED.value,
    FeedbackType.NEUTRAL.value: RecommendationEventType.NEUTRAL.value,
    FeedbackType.DISLIKE.value: RecommendationEventType.DISLIKED.value,
}

# BE#67 — weak confidence for a feedback-derived preference. The preference write
# path caps non-explicit sources at 0.4 regardless; this stays under it.
_FEEDBACK_PREFERENCE_CONFIDENCE = 0.3


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
        # BE#67 — also persist the outfit's item-set signature so the suppression
        # learner can match "the same look" across the new outfit row generated
        # each day (events carry only outfit_id, which is fresh per generation).
        event_type = _FEEDBACK_EVENT_MAP.get(feedback_type)
        if event_type is not None:
            await self.repository.add_recommendation_event(
                RecommendationEvent(
                    user_id=user_id,
                    recommendation_id=outfit.daily_recommendation_id,
                    outfit_id=outfit_id,
                    event_type=event_type,
                    source="outfit_feedback",
                    event_metadata={
                        "feedback_type": feedback_type,
                        "reason_tags": reason_tags or [],
                        "item_ids": [str(oi.item_id) for oi in outfit.items],
                    },
                    created_at=datetime.now(UTC),
                )
            )
            # BE#67 — translate REPEATED feedback into controlled weak preferences.
            # Write-gated by the flag (new behavior → clean rollback). Fail-soft:
            # a learning error must never fail the feedback the user submitted.
            if get_settings().ENABLE_FEEDBACK_LEARNING:
                await self._apply_weak_preferences(user_id)

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

    async def _apply_weak_preferences(self, user_id: uuid.UUID) -> None:
        """BE#67 — derive durable preferences from REPEATED recent feedback and
        write them weakly (capped 0.4, source=feedback_signal). Fail-soft per row:
        the just-recorded feedback event must persist even if learning errors."""
        try:
            # Flush so the event just added is visible to the derivation read.
            await self.repository.session.flush()
            now = datetime.now(UTC)
            events = await self.repository.list_recommendation_events(
                user_id, since=now - timedelta(days=DEFAULT_SUPPRESSION_DAYS)
            )
            signals = derive_learning_signals(events, now=now)
        except Exception as exc:
            logger.warning("feedback_learning.derive_failed", user_id=str(user_id), error=str(exc))
            return
        if not signals.weak_preferences:
            return
        pref_repo = SqlAlchemyPreferenceMemoryRepository(self.repository.session)
        for pref in signals.weak_preferences:
            try:
                await record_preference(
                    pref_repo,
                    user_id=user_id,
                    preference_type=pref.preference_type,
                    preference_value=pref.preference_value,
                    polarity=pref.polarity,
                    confidence=_FEEDBACK_PREFERENCE_CONFIDENCE,
                    source=SOURCE_FEEDBACK,
                )
            except PreferenceMemoryError as exc:
                logger.warning(
                    "feedback_learning.preference_write_skipped",
                    user_id=str(user_id),
                    reason=str(exc),
                )
