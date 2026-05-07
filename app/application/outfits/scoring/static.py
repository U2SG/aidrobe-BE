"""Static scoring layer: computed once at outfit creation, persisted in
`outfits.score_breakdown_json`. MUST NOT depend on current item.status,
fit_profile, or wear_history — those belong to the dynamic layer."""

from collections.abc import Sequence
from typing import Any

import structlog

from app.application.outfits.scoring.rules import get_scoring_rules
from app.infrastructure.persistence.models.wardrobe import WardrobeItem

logger = structlog.get_logger(__name__)


def _color_harmony(items: Sequence[WardrobeItem]) -> float:
    """Coarse heuristic: how many distinct primary colors are in the outfit.
    Real palette analysis is for V4. Stable, deterministic, no I/O."""
    if not items:
        return 0.0
    colors = {it.color_main for it in items if it.color_main}
    n = len(colors)
    if n <= 2:
        return 1.0
    if n == 3:
        return 0.7
    return 0.4


def _formality_match(items: Sequence[WardrobeItem], outfit_type: str) -> float:
    """formality_score is 0..100. Outfit types we score against:
    - formal/business: prefer high formality (>=60)
    - casual/weekend: prefer low (<=40)
    - sport: prefer low
    Items with no formality_score don't penalize."""
    scored = [it.formality_score for it in items if it.formality_score is not None]
    if not scored:
        return 0.7  # neutral fallback rather than 0
    avg = sum(scored) / len(scored)
    if outfit_type in {"formal", "business", "work"}:
        return min(avg / 100.0, 1.0)
    if outfit_type in {"casual", "weekend", "sport", "lounge"}:
        return min((100 - avg) / 100.0, 1.0)
    return 0.8  # default outfit_type — agnostic


def _season_fit(items: Sequence[WardrobeItem], current_season: str | None) -> float:
    """Fraction of items whose season_tags include current_season or 'all_season'.
    None current_season → neutral 0.8."""
    if current_season is None or not items:
        return 0.8
    matches = 0
    for it in items:
        tags = it.season_tags or {}
        # season_tags JSONB shape (per V3 M14): {"tags": ["spring", ...]}
        tag_list = tags.get("tags", []) if isinstance(tags, dict) else []
        if current_season in tag_list or "all_season" in tag_list:
            matches += 1
        elif not tag_list:
            matches += 1  # no tags → don't penalize
    return matches / len(items)


def compute_static_breakdown(
    items: Sequence[WardrobeItem],
    outfit_type: str,
    current_season: str | None = None,
) -> dict[str, Any]:
    """Returns the dict to persist into `outfits.score_breakdown_json` along
    with a `score_rules_version` from the loaded yaml."""
    rules = get_scoring_rules()
    static = rules["static"]
    aesthetic_components = static["aesthetic"]["components"]

    color = _color_harmony(items)
    formality = _formality_match(items, outfit_type)
    season = _season_fit(items, current_season)

    aesthetic = (
        color * aesthetic_components["color_harmony"]
        + formality * aesthetic_components["formality_match"]
        + season * aesthetic_components["season_fit"]
    )

    base_per = static["prep_minutes_base"]["per_item_base"]
    cap = static["prep_minutes_base"]["cap_minutes"]
    prep_base = min(len(items) * base_per, cap)

    breakdown = {
        "aesthetic": {
            "value": round(aesthetic, 4),
            "components": {
                "color_harmony": round(color, 4),
                "formality_match": round(formality, 4),
                "season_fit": round(season, 4),
            },
        },
        "prep_minutes_base": prep_base,
    }
    logger.info(
        "scoring.computed",
        layer="static",
        outfit_type=outfit_type,
        rule_version=rules.get("version"),
        aesthetic=breakdown["aesthetic"]["value"],
        prep_minutes_base=prep_base,
    )
    return breakdown


def get_rules_version() -> str:
    return str(get_scoring_rules().get("version", "0"))
