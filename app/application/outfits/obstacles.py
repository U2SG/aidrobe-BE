"""Obstacle detection: dirty, lost, seasonal, stale, weather_mismatch.

Phase B added the lightweight kinds (dirty/lost/seasonal/stale).
Phase D added weather_mismatch (driven by `context_json` from D1) and
replacement-candidate ranking (driven by `find_replacements` cascade)."""

from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

import structlog
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.outfits.replacements import find_replacements
from app.application.outfits.scoring.rules import get_freshness_rules
from app.common.i18n import I18nMessage, make_message
from app.infrastructure.persistence.models.wardrobe import WardrobeItem

logger = structlog.get_logger(__name__)

# How fresh `context_json.fetched_at` must be before we'll use it for the
# weather_mismatch check. Older snapshots emit `context.weather_stale` and
# the check is skipped (better silent than wrong on a 6h-old forecast).
_WEATHER_FRESHNESS_HOURS = 6
# Rain-probability threshold above which a top-only / no-outerwear outfit
# triggers `weather_mismatch`. Conservative — we'd rather miss than nag.
_RAIN_FLAG_THRESHOLD = 0.6


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


def detect_weather_mismatch(
    items: Sequence[WardrobeItem],
    context_json: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Phase D — flag the outfit when current weather context disagrees with
    what the items can tolerate. Conservative for now: rain probability over
    the threshold and no outerwear in the outfit ⇒ flag.

    Returns dicts shaped like other obstacles (kind/severity/title) but
    targets the OUTFIT, not a single item. Currently uses `item_id=""` as a
    sentinel for outfit-level — clients should treat empty item_id as
    "applies to the whole look"."""
    if not context_json:
        return []
    fetched_at = context_json.get("fetched_at")
    if isinstance(fetched_at, str):
        try:
            fetched_dt = datetime.fromisoformat(fetched_at)
            if fetched_dt.tzinfo is None:
                fetched_dt = fetched_dt.replace(tzinfo=UTC)
            if datetime.now(UTC) - fetched_dt > timedelta(hours=_WEATHER_FRESHNESS_HOURS):
                logger.info("context.weather_stale", fetched_at=fetched_at)
                return []
        except ValueError:
            return []

    weather = context_json.get("weather")
    if not isinstance(weather, dict):
        return []

    rain_p = weather.get("rain_probability")
    if not isinstance(rain_p, int | float):
        return []
    if rain_p < _RAIN_FLAG_THRESHOLD:
        return []

    has_outerwear = any(getattr(i, "category", None) == "outerwear" for i in items)
    if has_outerwear:
        return []

    rules = get_freshness_rules()
    rule_version = str(rules.get("version", "0"))
    msg = make_message(
        "obstacle.weather_mismatch",
        f"今天大概率有雨（{int(rain_p * 100)}%），加件外套吧",
        {"rain_probability": rain_p},
        rule_version=rule_version,
    )
    return [
        {
            "kind": "weather_mismatch",
            "item_id": "",
            "severity": "warning",
            "title": msg.model_dump(mode="json"),
        }
    ]


async def attach_replacement_candidates(
    session: AsyncSession,
    obstacles: list[dict[str, Any]],
    items: Sequence[WardrobeItem],
    *,
    k: int = 3,
) -> list[dict[str, Any]]:
    """Augment item-targeted obstacles with `replacement_candidate_ids`.
    Stale obstacles are skipped (suggesting a freshly-worn item to replace
    a freshly-worn item is unhelpful), as are weather_mismatch entries
    (no single item is the problem).

    Batched: instead of a per-obstacle find_replacements (which is up to 3
    SQL queries each, ⇒ N×3 queries on /today GET), we collect every
    obstacle's category in one go and run a single category-IN query, then
    rank in-memory per obstacle. Embedding-similarity tier (which is
    per-item by nature) still takes its own query when pgvector is on,
    but tier-2/tier-3 fallbacks become O(1) total queries instead of O(N)."""
    from app.application.outfits.replacements import find_replacements_batch

    items_by_id = {str(i.id): i for i in items}
    targeted = [
        o
        for o in obstacles
        if o.get("kind") not in {"stale", "weather_mismatch"} and o.get("item_id")
    ]
    targeted_items = [
        items_by_id[o["item_id"]] for o in targeted if o["item_id"] in items_by_id
    ]
    if not targeted_items:
        return obstacles

    other_outfit_item_ids = {i.id for i in items}
    candidates_by_item = await find_replacements_batch(
        session, targeted_items, exclude_ids=other_outfit_item_ids, k=k
    )
    for o in targeted:
        item_id = o["item_id"]
        if item_id not in items_by_id:
            continue
        item = items_by_id[item_id]
        ids = candidates_by_item.get(item.id, [])
        if not ids:
            # Cascade fall-through to the per-item function so embedding-
            # tier (when enabled) still runs. The per-item path is the
            # exception, not the rule, so we don't lose the batched win.
            ids = await find_replacements(
                session, item, exclude_ids=other_outfit_item_ids, k=k
            )
        o["replacement_candidate_ids"] = [str(c) for c in ids]
    return obstacles
