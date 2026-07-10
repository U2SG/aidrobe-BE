import uuid
from datetime import UTC, datetime
from typing import Any

import structlog

from app.infrastructure.persistence.models.recommendation import RecommendationEvent
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)

logger = structlog.get_logger(__name__)


class IngestRecommendationEvent:
    """BE#81 — general passive-learning event ingestion.

    Append-only capture into the RecommendationEvent stream. It deliberately does
    NOT touch explicit preferences (OutfitFeedback / FitFeedback / preference
    memory): weak/inferred signals must not overwrite what the user stated
    explicitly. Aggregation / confidence-gated inference (e.g. #82 indoor-climate)
    reads this stream later — ingestion just records raw material.
    """

    def __init__(self, repository: SqlAlchemyRecommendationRepository) -> None:
        self.repository = repository

    async def execute(
        self,
        user_id: uuid.UUID,
        *,
        event_type: str,
        signal_strength: str,
        source_surface: str,
        outfit_id: uuid.UUID | None = None,
        recommendation_id: uuid.UUID | None = None,
        item_id: uuid.UUID | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> RecommendationEvent:
        event = RecommendationEvent(
            user_id=user_id,
            recommendation_id=recommendation_id,
            outfit_id=outfit_id,
            item_id=item_id,
            event_type=event_type,
            signal_strength=signal_strength,
            source_surface=source_surface,
            event_metadata=metadata or None,
            created_at=datetime.now(UTC),
        )
        stored = await self.repository.add_recommendation_event(event)
        logger.info(
            "passive_event_ingested",
            user_id=str(user_id),
            event_type=event_type,
            signal_strength=signal_strength,
            source_surface=source_surface,
        )
        return stored
