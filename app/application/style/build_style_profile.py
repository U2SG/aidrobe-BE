"""Aggregate wear history, feedback, and inspirations into a single style signal.

The result is a weight list used by the recommendation engine's aesthetic_score.
Without pgvector, we represent the profile as weighted tag/color counters instead of
a real embedding average. The engine's aesthetic_score uses cosine similarity over
these sparse feature dicts.
"""

from collections import defaultdict
from typing import Any

from app.common.enums import FeedbackType
from app.infrastructure.persistence.models.recommendation import OutfitFeedback, WearHistory
from app.infrastructure.persistence.models.style import UserStyleProfile
from app.infrastructure.persistence.models.wardrobe import WardrobeItem


def _feedback_weight(feedback_type: str) -> float:
    if feedback_type == FeedbackType.LOVE.value:
        return 1.5
    elif feedback_type == FeedbackType.LIKE.value:
        return 1.0
    elif feedback_type == FeedbackType.NEUTRAL.value:
        return 0.0
    elif feedback_type == FeedbackType.DISLIKE.value:
        return -1.0
    raise ValueError(f"Unknown feedback_type: {feedback_type}")


def build_style_profile(
    style_profiles: list[UserStyleProfile],
    wear_history: list[WearHistory],
    feedback: list[OutfitFeedback],
    wardrobe_items: list[WardrobeItem],
    outfit_main_item_ids: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Return a profile dict the engine can use for aesthetic_score.

    outfit_main_item_ids maps outfit_id → wardrobe_item_id for worn outfits,
    allowing wear history signals to be resolved to item colors/categories.

    Shape:
      {
        "color_weights": {"black": 1.6, "white": 0.5, ...},
        "category_weights": {"top": 0.9, ...},
        "tag_weights": {"minimal": 0.7, ...},
      }
    """
    item_by_id = {str(item.id): item for item in wardrobe_items}
    outfit_main_item_ids = outfit_main_item_ids or {}

    color_counter: defaultdict[str, float] = defaultdict(float)
    category_counter: defaultdict[str, float] = defaultdict(float)

    feedback_weights_by_outfit_id = {
        str(fb.outfit_id): _feedback_weight(fb.feedback_type) for fb in feedback
    }

    for wh in wear_history:
        outfit_id_str = str(wh.outfit_id)
        item_id_str = outfit_main_item_ids.get(outfit_id_str)
        if item_id_str is None:
            continue
        item = item_by_id.get(item_id_str)
        if item is None:
            continue
        weight = feedback_weights_by_outfit_id.get(outfit_id_str, 0.8)
        color_counter[item.color_main] += weight
        category_counter[item.category] += weight

    tag_counter: defaultdict[str, float] = defaultdict(float)
    for profile in style_profiles:
        if profile.style_tags and profile.source_type in {"inspiration_image", "style_tag"}:
            tags = profile.style_tags.get("tags", [])
            for tag in tags:
                tag_counter[tag] += float(profile.weight)

    return {
        "color_weights": dict(color_counter),
        "category_weights": dict(category_counter),
        "tag_weights": dict(tag_counter),
    }


def aesthetic_score(
    outfit_colors: list[str],
    outfit_categories: list[str],
    profile: dict[str, Any],
) -> float:
    """Cosine-like similarity between outfit features and user style profile.

    Returns 0.0 (neutral, excluded from scoring) when profile is empty.
    """
    color_weights: dict[str, float] = profile.get("color_weights", {})
    category_weights: dict[str, float] = profile.get("category_weights", {})

    if not color_weights and not category_weights:
        return 0.0

    color_dot = sum(color_weights.get(c, 0.0) for c in outfit_colors)
    color_norm = (sum(v * v for v in color_weights.values()) ** 0.5) or 1.0
    outfit_color_norm = (len(outfit_colors) ** 0.5) or 1.0
    color_sim = color_dot / (color_norm * outfit_color_norm)

    cat_dot = sum(category_weights.get(c, 0.0) for c in outfit_categories)
    cat_norm = (sum(v * v for v in category_weights.values()) ** 0.5) or 1.0
    outfit_cat_norm = (len(outfit_categories) ** 0.5) or 1.0
    cat_sim = cat_dot / (cat_norm * outfit_cat_norm)

    return min(1.0, max(0.0, (color_sim + cat_sim) / 2))
