"""The editable experience of actually wearing one saved outfit."""

import uuid
from dataclasses import dataclass
from typing import Literal, cast

from app.application.recommendations.feedback_tags import get_tags_for_scene
from app.common import error_codes
from app.common.enums import FitScene
from app.common.exceptions import FriendlyException
from app.common.local_time import utc_now
from app.infrastructure.persistence.models.recommendation import OutfitFeedback
from app.infrastructure.persistence.repositories.recommendation_repo import (
    WEAR_FEEDBACK_TYPE,
    SqlAlchemyRecommendationRepository,
)

WearFeedbackRating = Literal["like", "dislike"]


@dataclass(frozen=True)
class WearFeedbackDetails:
    feedback_type: WearFeedbackRating
    reason_tags: list[str]
    note: str | None


def wear_feedback_details(row: OutfitFeedback) -> WearFeedbackDetails:
    data = row.reason_tags or {}
    return WearFeedbackDetails(
        feedback_type=cast(WearFeedbackRating, data["feedback_type"]),
        reason_tags=cast(list[str], data["tags"]),
        note=cast(str | None, data.get("note")),
    )


class WearFeedback:
    def __init__(self, repository: SqlAlchemyRecommendationRepository) -> None:
        self.repository = repository

    async def _require_outfit(self, outfit_id: uuid.UUID, user_id: uuid.UUID, *, lock: bool = False) -> None:
        if not await self.repository.owns_feedback_outfit(outfit_id, user_id, lock=lock):
            raise FriendlyException(error_codes.NOT_FOUND, "Outfit not found", status_code=404)

    async def read(self, outfit_id: uuid.UUID, user_id: uuid.UUID) -> WearFeedbackDetails | None:
        await self._require_outfit(outfit_id, user_id)
        row = await self.repository.get_wear_feedback_for_user(outfit_id, user_id)
        return wear_feedback_details(row) if row is not None else None

    async def save(
        self, outfit_id: uuid.UUID, user_id: uuid.UUID, details: WearFeedbackDetails,
    ) -> WearFeedbackDetails:
        await self._require_outfit(outfit_id, user_id, lock=True)
        overall_tags = {
            tag.key
            for scene in FitScene
            for tag in get_tags_for_scene(scene.value)
            if "overall" in tag.applies_to
        }
        if any(tag not in overall_tags for tag in details.reason_tags):
            raise FriendlyException(
                error_codes.VALIDATION_ERROR, "Invalid wear feedback tag", status_code=422
            )
        details = WearFeedbackDetails(
            details.feedback_type,
            list(dict.fromkeys(details.reason_tags)),
            (details.note or "").strip() or None,
        )
        existing = await self.repository.get_wear_feedback_for_user(outfit_id, user_id)
        if existing is not None and wear_feedback_details(existing) == details:
            return details
        now = utc_now()
        data = {
            "feedback_type": details.feedback_type,
            "tags": details.reason_tags,
            "note": details.note,
            "updated_at": now.isoformat(timespec="microseconds"),
        }
        if existing is None:
            await self.repository.add_feedback(OutfitFeedback(
                user_id=user_id,
                outfit_id=outfit_id,
                feedback_type=WEAR_FEEDBACK_TYPE,
                reason_tags=data,
                created_at=now,
            ))
        else:
            existing.reason_tags = data
        return details
