"""Build the privacy-safe recommendation decision trace payload.

The builder is pure: it receives the already-resolved engine inputs and returns
JSON-compatible data without reading a database or calling a provider. Raw
prompts, user override text, item titles, image/OCR data, and URLs are excluded
from its output. Wardrobe location equality is retained through opaque per-trace
buckets rather than persisted location text.

Retention deliberately follows the owning ``daily_recommendations`` row. The
``recommendation_traces.daily_recommendation_id`` foreign key uses ON DELETE
CASCADE, so revision cleanup, clear-all-data, and account purge remove the trace
with the recommendation.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from app.application.ports.weather import WeatherInfo
from app.application.recommendations.context_override import (
    CONSTRAINT_TAXONOMY,
    EXCLUDE_DRESS,
)
from app.application.recommendations.engine import (
    EngineOutfit,
    EngineResult,
    RelationshipEdge,
)
from app.application.recommendations.indoor_climate import IndoorClimateSignal
from app.infrastructure.persistence.models.base import JsonDict
from app.infrastructure.persistence.models.preference_memory import UserPreferenceMemory
from app.infrastructure.persistence.models.wardrobe import WardrobeItem

TRACE_SCHEMA_VERSION = 1
ENGINE_VERSION = "recommendation-engine-v1"
RANKER_SCHEMA_VERSION = "recommendation-ranker-v1"
PROMPT_SCHEMA_VERSION = "recommendation-reason-prompt-v1"
REASON_OUTPUT_SCHEMA_VERSION = "recommendation-reason-output-v1"
_MEMORY_TYPES = frozenset(
    {
        "brand",
        "climate",
        "color",
        "fit",
        "footwear",
        "formality",
        "material",
        "negative_preference",
        "occasion",
        "style",
    }
)
_MEMORY_SOURCES = frozenset(
    {"explicit_user_statement", "feedback_signal", "inferred_from_history"}
)


def build_decision_trace_payload(
    *,
    target_date: date,
    captured_at: datetime,
    timezone_bucket: str,
    weather: WeatherInfo | None,
    weather_resolved_at: str | None,
    commute_exposure: str | None,
    indoor_climate: IndoorClimateSignal | None,
    normalized_constraints: Sequence[str],
    wardrobe_items: Sequence[WardrobeItem],
    memory_items: Sequence[UserPreferenceMemory],
    engine_result: EngineResult,
    recent_worn_main_item_ids: set[str],
    recent_outfit_item_ids: Sequence[set[str]],
    style_profile: Mapping[str, Any],
    fit_profile: Mapping[str, Any],
    disliked_colors: set[str],
    relationship_edges: Sequence[RelationshipEdge],
    suppressed_outfit_item_ids: Sequence[set[str]],
    avoid_exact_repeats: bool,
    persisted_rules_version: str | None,
    llm_provider: str | None,
    llm_model: str | None,
    deterministic_seed: int | None,
) -> JsonDict:
    """Return schema-v1 trace data containing only replay-safe normalized facts."""
    constraints = sorted(set(normalized_constraints) & CONSTRAINT_TAXONOMY)
    wardrobe = _wardrobe_snapshot(
        wardrobe_items,
        recent_worn_main_item_ids,
        recent_outfit_item_ids,
        relationship_edges,
        suppressed_outfit_item_ids,
    )
    wardrobe_colors = {
        color
        for item in wardrobe["items"]
        if isinstance(item, dict)
        for color in (item.get("color"), item.get("secondary_color"))
        if isinstance(color, str) and color
    }
    return {
        "schema_version": TRACE_SCHEMA_VERSION,
        "context": {
            "target_date": target_date.isoformat(),
            "captured_at": captured_at.isoformat(),
            "timezone_bucket": timezone_bucket,
            "weather": _weather_snapshot(weather, weather_resolved_at),
            "commute_exposure": _normalized_identifier(commute_exposure),
            "indoor_climate": dataclasses.asdict(indoor_climate)
            if indoor_climate is not None
            else None,
            "normalized_constraints": constraints,
            "avoid_exact_repeats": avoid_exact_repeats,
        },
        "wardrobe": wardrobe,
        "memory": {
            "items": [
                {
                    "id": str(item.id),
                    "type": _allowlisted_identifier(
                        item.preference_type, _MEMORY_TYPES
                    ),
                    "confidence": float(item.confidence),
                    "source": _allowlisted_identifier(item.source, _MEMORY_SOURCES),
                }
                for item in sorted(memory_items, key=lambda row: str(row.id))
            ],
        },
        "candidate_trace": _candidate_snapshot(
            engine_result,
            constraints=constraints,
            wardrobe_items=wardrobe_items,
            recent_outfit_item_ids=recent_outfit_item_ids,
            avoid_exact_repeats=avoid_exact_repeats,
        ),
        "provenance": {
            "engine_version": ENGINE_VERSION,
            "rules_version": persisted_rules_version,
            "ranker_schema_version": RANKER_SCHEMA_VERSION,
            "prompt_schema_version": PROMPT_SCHEMA_VERSION,
            "replay_parameters": {
                "style_profile": _safe_style_profile(style_profile),
                "fit_profile": _safe_fit_profile(fit_profile),
                "disliked_color_ids": sorted(
                    color
                    for color in (_normalized_identifier(value) for value in disliked_colors)
                    if color is not None and color in wardrobe_colors
                ),
            },
            "llm": (
                {
                    "provider": _normalized_identifier(llm_provider),
                    "model": _normalized_identifier(llm_model),
                }
                if llm_provider is not None
                else None
            ),
            "deterministic_seed": deterministic_seed,
            "reason_output_schema_version": REASON_OUTPUT_SCHEMA_VERSION,
        },
    }


def _weather_snapshot(
    weather: WeatherInfo | None, resolved_at: str | None
) -> JsonDict | None:
    if weather is None:
        return None
    return {
        "facts": {
            "temperature_c": weather.temperature_c,
            "feels_like_c": weather.feels_like_c,
            "rain_probability": weather.rain_probability,
            "condition": _normalized_identifier(weather.condition) or "unknown",
            "humidity": weather.humidity,
            "wind_dir": _normalized_identifier(weather.wind_dir),
            "wind_scale": _normalized_identifier(weather.wind_scale),
            "wind_speed": weather.wind_speed,
            "icon": _normalized_identifier(weather.icon),
        },
        "provider_or_cache_resolved_at": resolved_at,
    }


def _wardrobe_snapshot(
    items: Sequence[WardrobeItem],
    recent_main_ids: set[str],
    recent_outfits: Sequence[set[str]],
    relationships: Sequence[RelationshipEdge],
    suppressed_outfits: Sequence[set[str]],
) -> JsonDict:
    location_buckets: dict[str, str] = {}

    def location_bucket(location: str | None) -> str | None:
        if not location:
            return None
        if location not in location_buckets:
            location_buckets[location] = f"location_{len(location_buckets) + 1}"
        return location_buckets[location]

    return {
        "items": [
            {
                "id": str(item.id),
                "category": _normalized_identifier(item.category),
                "subcategory": _normalized_identifier(item.sub_category),
                "color": _normalized_identifier(item.color_main),
                "secondary_color": _normalized_identifier(item.color_secondary),
                "style_features": _taxonomy_tokens(item.style_tags),
                "season_features": _taxonomy_tokens(item.season_tags),
                "fit_status": _normalized_identifier(item.fit_status),
                "fit_intent": _normalized_identifier(item.fit_intent),
                "status": _normalized_identifier(item.status),
                "availability": (
                    "available"
                    if item.status == "wearable"
                    and item.deleted_at is None
                    and item.fit_status != "no_longer_fits"
                    else "unavailable"
                ),
                "location_bucket": location_bucket(item.location),
                "last_worn_on": item.last_worn_at.isoformat()
                if item.last_worn_at is not None
                else None,
                "wear_count": item.wear_count,
            }
            for item in items
        ],
        "recent_worn_main_item_ids": sorted(recent_main_ids),
        "recent_outfit_item_ids": _sorted_item_sets(recent_outfits),
        "relationships": [
            {
                "source_item_id": edge.source_item_id,
                "target_item_id": edge.target_item_id,
                "type": _normalized_identifier(edge.relationship_type),
                "score": edge.score,
                "confidence": edge.confidence,
            }
            for edge in relationships
        ],
        "suppressed_outfit_item_ids": _sorted_item_sets(suppressed_outfits),
    }


def _candidate_snapshot(
    result: EngineResult,
    *,
    constraints: Sequence[str],
    wardrobe_items: Sequence[WardrobeItem],
    recent_outfit_item_ids: Sequence[set[str]],
    avoid_exact_repeats: bool,
) -> JsonDict:
    selected = {
        _candidate_key(outfit): rank
        for rank, outfit in enumerate(result.outfits, start=1)
    }
    ranks = _candidate_ranks(result.candidates)
    recent_sets = {frozenset(item_ids) for item_ids in recent_outfit_item_ids}
    candidates: list[JsonDict] = []
    for candidate in result.candidates:
        key = _candidate_key(candidate)
        selected_rank = selected.get(key)
        rejection_reasons: list[str] = []
        if selected_rank is None:
            if avoid_exact_repeats and frozenset(candidate.item_ids) in recent_sets:
                rejection_reasons.append("recent_exact_repeat")
            if candidate.score_breakdown.get("wardrobe_graph_penalty", 0.0) > 0:
                rejection_reasons.append("relationship_rejection")
            if candidate.score_breakdown.get("feedback_suppression_penalty", 0.0) > 0:
                rejection_reasons.append("feedback_suppressed")
            if not rejection_reasons:
                rejection_reasons.append("not_selected")
        candidates.append(
            {
                "outfit_type": candidate.outfit_type,
                "item_ids": list(candidate.item_ids),
                "score_total": candidate.score_total,
                "score_breakdown": dict(candidate.score_breakdown),
                "filter_rejection_reasons": rejection_reasons,
                "rank_within_type": ranks[key],
                "selected_rank": selected_rank,
            }
        )
    filtered_items: list[JsonDict] = []
    if EXCLUDE_DRESS in constraints:
        filtered_items = [
            {"item_id": str(item.id), "reasons": ["context_exclude_dress"]}
            for item in wardrobe_items
            if item.category == "dress" or item.sub_category == "skirt"
        ]
    return {
        "status": result.status,
        "filtered_items": filtered_items,
        "candidates": candidates,
    }


def _candidate_ranks(
    candidates: Sequence[EngineOutfit],
) -> dict[tuple[str, tuple[str, ...]], int]:
    ranks: dict[tuple[str, tuple[str, ...]], int] = {}
    outfit_types = sorted({candidate.outfit_type for candidate in candidates})
    for outfit_type in outfit_types:
        pool = [candidate for candidate in candidates if candidate.outfit_type == outfit_type]
        ordered = sorted(enumerate(pool), key=lambda pair: (-pair[1].score_total, pair[0]))
        for rank, (_, candidate) in enumerate(ordered, start=1):
            ranks[_candidate_key(candidate)] = rank
    return ranks


def _candidate_key(candidate: EngineOutfit) -> tuple[str, tuple[str, ...]]:
    return candidate.outfit_type, tuple(candidate.item_ids)


def _safe_style_profile(profile: Mapping[str, Any]) -> JsonDict:
    return {
        "color_weights": _weight_map(profile.get("color_weights")),
        "category_weights": _weight_map(profile.get("category_weights")),
    }


def _safe_fit_profile(profile: Mapping[str, Any]) -> JsonDict:
    category_preferences: dict[str, str] = {}
    raw_preferences = profile.get("category_preferences")
    if isinstance(raw_preferences, dict):
        for key, value in raw_preferences.items():
            normalized_key = _taxonomy_identifier(key)
            normalized_value = _normalized_identifier(value)
            if normalized_key is not None and normalized_value is not None:
                category_preferences[normalized_key] = normalized_value
    else:
        for key, value in profile.items():
            normalized_key = _normalized_identifier(key)
            normalized_value = _normalized_identifier(value)
            if normalized_key is not None and normalized_value is not None:
                category_preferences[normalized_key] = normalized_value
    body_shape_tag_weights = _weight_map(profile.get("body_shape_tag_weights"))
    safe_profile: JsonDict = {}
    if category_preferences:
        safe_profile["category_preferences"] = category_preferences
    if body_shape_tag_weights:
        safe_profile["body_shape_tag_weights"] = body_shape_tag_weights
    if profile and not safe_profile:
        # The engine's has-fit-signal branch checks bool(fit_profile). Preserve
        # that behavior without persisting unknown/raw profile fields.
        safe_profile["signal_present"] = True
    return safe_profile


def _weight_map(value: Any) -> dict[str, float]:
    if not isinstance(value, dict):
        return {}
    weights: dict[str, float] = {}
    for key, weight in value.items():
        normalized_key = _normalized_identifier(key)
        if (
            normalized_key is not None
            and isinstance(weight, int | float | Decimal)
            and not isinstance(weight, bool)
        ):
            weights[normalized_key] = float(weight)
    return weights


def _taxonomy_tokens(value: Any) -> list[str]:
    tokens: set[str] = set()
    if isinstance(value, dict):
        for key, nested in value.items():
            normalized_key = _taxonomy_identifier(key)
            if nested is True and normalized_key is not None:
                tokens.add(normalized_key)
            tokens.update(_taxonomy_tokens(nested))
    elif isinstance(value, list | tuple | set):
        for nested in value:
            tokens.update(_taxonomy_tokens(nested))
    elif isinstance(value, str):
        token = _taxonomy_identifier(value)
        if token is not None:
            tokens.add(token)
    return sorted(tokens)


def _normalized_identifier(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().lower()
    if not normalized or len(normalized) > 80:
        return None
    if "://" in normalized or normalized.startswith("data:"):
        return None
    return normalized


def _taxonomy_identifier(value: Any) -> str | None:
    normalized = _normalized_identifier(value)
    if normalized is None or re.fullmatch(r"[a-z0-9][a-z0-9_:-]{0,79}", normalized) is None:
        return None
    if any(forbidden in normalized for forbidden in ("image", "url", "ocr", "prompt")):
        return None
    return normalized


def _allowlisted_identifier(value: Any, allowed: frozenset[str]) -> str:
    normalized = _normalized_identifier(value)
    return normalized if normalized in allowed else "unknown"


def _sorted_item_sets(item_sets: Sequence[set[str]]) -> list[list[str]]:
    return sorted(
        (sorted(item_ids) for item_ids in item_sets),
        key=lambda item_ids: tuple(item_ids),
    )
