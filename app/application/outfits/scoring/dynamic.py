"""Dynamic scoring layer: computed at GET time, NOT persisted. Reflects
current item.status, fit_profile, and freshness. Combined with the persisted
static breakdown to produce the final `OutfitScores`."""

from collections.abc import Sequence
from typing import Any

import structlog
from pydantic import BaseModel

from app.application.outfits.scoring.rules import get_scoring_rules
from app.infrastructure.persistence.models.wardrobe import WardrobeItem

logger = structlog.get_logger(__name__)


class OutfitScores(BaseModel):
    aesthetic: float
    feasibility: float
    fit: float
    total: float
    prep_minutes: int
    rules_version: str


def _feasibility(items: Sequence[WardrobeItem], blocking: set[str]) -> float:
    if not items:
        return 0.0
    return 0.0 if any(it.status in blocking for it in items) else 1.0


def _fit(items: Sequence[WardrobeItem], fit_map: dict[str, float], fallback: float) -> float:
    if not items:
        return fallback
    scores = [fit_map.get(it.fit_status, fit_map.get("unknown", fallback)) for it in items]
    return sum(scores) / len(scores)


def _prep_minutes(
    items: Sequence[WardrobeItem],
    base_minutes: int,
    modifiers: dict[str, Any],
    has_dirty_obstacle: bool,
) -> int:
    extra = 0.0
    locations = {it.location for it in items if it.location}
    if len(locations) > 1:
        extra += (len(locations) - 1) * modifiers["different_locations_per_extra"]
    delicate = {"silk", "linen", "wool", "leather", "cashmere"}
    if any((it.material or "").lower() in delicate for it in items):
        extra += modifiers["has_delicate_material"]
    if has_dirty_obstacle:
        extra += modifiers["has_dirty_obstacle"]
    return int(round(base_minutes + extra))


def compute_dynamic_scores(
    items: Sequence[WardrobeItem],
    static_breakdown: dict[str, Any] | None,
    *,
    has_dirty_obstacle: bool = False,
) -> OutfitScores:
    """`static_breakdown` is the JSONB persisted by `compute_static_breakdown`.
    If it's None (legacy outfit before B1), we degrade to aesthetic=0.5,
    prep_base=2 — explicit fallback rather than crashing."""
    rules = get_scoring_rules()
    static_cfg = rules["static"]
    dyn = rules["dynamic"]

    aesthetic_value = (
        float(static_breakdown.get("aesthetic", {}).get("value", 0.5))
        if static_breakdown
        else 0.5
    )
    prep_base = int(static_breakdown.get("prep_minutes_base", 2)) if static_breakdown else 2

    feasibility = _feasibility(items, set(dyn["feasibility"]["blocking_statuses"]))
    fit = _fit(
        items,
        dict(dyn["fit"]["fit_score_map"]),
        float(dyn["fit"]["fallback_when_no_profile"]),
    )
    prep_minutes = _prep_minutes(
        items,
        prep_base,
        dyn["prep_minutes_modifiers"],
        has_dirty_obstacle,
    )

    w_aesthetic = float(static_cfg["aesthetic"]["weight"])
    w_feasibility = float(dyn["feasibility"]["weight"])
    w_fit = float(dyn["fit"]["weight"])
    total = w_aesthetic * aesthetic_value + w_feasibility * feasibility + w_fit * fit
    total = max(0.0, min(1.0, total))

    scores = OutfitScores(
        aesthetic=round(aesthetic_value, 4),
        feasibility=round(feasibility, 4),
        fit=round(fit, 4),
        total=round(total, 4),
        prep_minutes=prep_minutes,
        rules_version=str(rules.get("version", "0")),
    )
    logger.info(
        "scoring.computed",
        layer="dynamic",
        rule_version=scores.rules_version,
        feasibility=scores.feasibility,
        fit=scores.fit,
        total=scores.total,
        prep_minutes=scores.prep_minutes,
        items_count=len(items),
    )
    return scores
