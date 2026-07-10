"""Pick at most one "you haven't worn this in a while, and it fits today"
resurface insight for the Today response (BE#80).

Rules baked in (per the issue):
- Never fabricated: returns None when there's no genuine forgotten + season-
  suitable candidate.
- One per response; most-forgotten first (never-worn before long-unworn).
- BE-side anti-spam: items already surfaced recently (FE emits
  resurface_insight_viewed) are skipped, on top of FE's own 14-day cooldown.
- Copy is non-judgmental — no guilt/shame ("浪费" / "你买了却没穿").
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)

# FE also caps same-item cooldown at 14 days; BE mirrors it so the two agree.
_RESURFACE_COOLDOWN_DAYS = 14
_INSIGHT_TEXT = "这件你有阵子没穿了，今天正合适。"
_REASON = "unworn_suitable_today"


@dataclass(frozen=True)
class ResurfaceInsight:
    item_id: uuid.UUID
    text: str
    reason: str


def _season_suitable(item: WardrobeItem, current_season: str | None) -> bool:
    if current_season is None:
        return True
    tags = item.season_tags or {}
    tag_list = tags.get("tags", []) if isinstance(tags, dict) else []
    # No season info → don't exclude (we can't claim it's unsuitable).
    if not tag_list:
        return True
    return current_season in tag_list or "all_season" in tag_list


async def build_resurface_insight(
    repository: SqlAlchemyRecommendationRepository,
    user_id: uuid.UUID,
    current_season: str | None,
    *,
    now: datetime,
    min_unworn_days: int,
) -> ResurfaceInsight | None:
    cutoff = now.date() - timedelta(days=min_unworn_days)
    candidates = await repository.list_forgotten_wearable_items(user_id, cutoff)
    if not candidates:
        return None
    excluded = await repository.recently_resurfaced_item_ids(
        user_id, now - timedelta(days=_RESURFACE_COOLDOWN_DAYS)
    )
    for item in candidates:
        if item.id in excluded:
            continue
        if not _season_suitable(item, current_season):
            continue
        return ResurfaceInsight(item_id=item.id, text=_INSIGHT_TEXT, reason=_REASON)
    return None
