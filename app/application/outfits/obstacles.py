"""Lightweight obstacle detection: dirty, lost, seasonal, stale.

Phase B is intentionally limited:
- weather_mismatch lands in Phase D (needs context wiring)
- replacement candidate ranking lands in Phase D (needs embedding)
"""

from collections.abc import Sequence
from datetime import date
from typing import Any

import structlog
from pydantic import BaseModel

from app.application.outfits.scoring.rules import get_freshness_rules
from app.common.i18n import I18nMessage, make_message
from app.infrastructure.persistence.models.wardrobe import WardrobeItem

logger = structlog.get_logger(__name__)


class Obstacle(BaseModel):
    kind: str  # laundry | lost | seasonal | stale
    item_id: str
    severity: str  # blocking | warning | info
    title: I18nMessage


def _seasonal_mismatch(item: WardrobeItem, current_season: str | None) -> bool:
    # Falsy guard catches None, "", and any other empty value — an empty string
    # would otherwise flag every non-all_season item as mismatched.
    if not current_season:
        return False
    tags = item.season_tags or {}
    tag_list = tags.get("tags", []) if isinstance(tags, dict) else []
    if not tag_list:
        return False
    return current_season not in tag_list and "all_season" not in tag_list


def detect_obstacles(
    items: Sequence[WardrobeItem],
    *,
    today: date,
    current_season: str | None = None,
) -> list[dict[str, Any]]:
    """Returns a list of obstacle dicts (already JSON-serialized) ready to
    persist into `outfits.obstacles_json` AND/OR return via OutfitResponse."""
    rules = get_freshness_rules()
    stale_threshold = int(rules.get("stale_threshold_days", 90))
    rule_version = str(rules.get("version", "0"))

    out: list[Obstacle] = []
    for it in items:
        if it.status == "dirty":
            out.append(
                Obstacle(
                    kind="laundry",
                    item_id=str(it.id),
                    severity="blocking",
                    title=make_message(
                        "obstacle.laundry",
                        f"{it.title}在脏衣篮",
                        {"item_title": it.title},
                        rule_version=rule_version,
                    ),
                )
            )
            continue
        if it.status == "lost":
            out.append(
                Obstacle(
                    kind="lost",
                    item_id=str(it.id),
                    severity="blocking",
                    title=make_message(
                        "obstacle.lost",
                        f"{it.title}已标记为丢失",
                        {"item_title": it.title},
                        rule_version=rule_version,
                    ),
                )
            )
            continue
        if _seasonal_mismatch(it, current_season):
            out.append(
                Obstacle(
                    kind="seasonal",
                    item_id=str(it.id),
                    severity="warning",
                    title=make_message(
                        "obstacle.seasonal",
                        f"{it.title}不太适合{current_season or ''}",
                        {"item_title": it.title, "season": current_season or ""},
                        rule_version=rule_version,
                    ),
                )
            )
            continue
        if it.last_worn_at is not None and (today - it.last_worn_at).days > stale_threshold:
            days = (today - it.last_worn_at).days
            out.append(
                Obstacle(
                    kind="stale",
                    item_id=str(it.id),
                    severity="info",
                    title=make_message(
                        "obstacle.stale",
                        f"{it.title}已经{days}天没穿过，建议先晾一下",
                        {"item_title": it.title, "days": days},
                        rule_version=rule_version,
                    ),
                )
            )

    serialized = [o.model_dump(mode="json") for o in out]
    logger.info(
        "obstacle.detected",
        rule_version=rule_version,
        count=len(serialized),
        kinds=sorted({o.kind for o in out}),
    )
    return serialized


def has_blocking(obstacles: Sequence[dict[str, Any]]) -> bool:
    return any(o.get("severity") == "blocking" for o in obstacles)


def has_dirty(obstacles: Sequence[dict[str, Any]]) -> bool:
    return any(o.get("kind") == "laundry" for o in obstacles)
