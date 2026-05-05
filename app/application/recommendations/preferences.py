from collections import Counter
from typing import Any

from app.infrastructure.persistence.models.recommendation import OutfitFeedback, WearHistory


def build_preference_snapshot(
    wear_history: list[WearHistory],
    feedback: list[OutfitFeedback],
) -> dict[str, Any]:
    return {
        "wear_frequency": Counter(str(row.outfit_id) for row in wear_history),
        "disliked_outfits": Counter(
            str(row.outfit_id) for row in feedback if row.feedback_type == "dislike"
        ),
    }
