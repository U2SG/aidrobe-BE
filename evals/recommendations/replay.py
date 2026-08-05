"""Deterministic replay helpers for stored recommendation decision traces.

This eval-side module may import production application code; production code
must never import this module. Replays always disable the LLM boundary so only
deterministic selection, score, and structured reasoning behavior is compared.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, cast

from app.application.ports.weather import WeatherInfo
from app.application.recommendations.decision_trace import TRACE_SCHEMA_VERSION
from app.application.recommendations.engine import (
    EngineItem,
    EngineResult,
    RecommendationEngine,
    RelationshipEdge,
)
from app.application.recommendations.indoor_climate import IndoorClimateSignal
from app.infrastructure.persistence.models.recommendation import RecommendationTrace
from evals.recommendations.runner import (
    EvaluationError,
    RecommendationEngineParameters,
    RecommendationRunnerInput,
    run_recommendation_input,
)


class ReplayError(ValueError):
    """The stored trace cannot be reconstructed into an engine input."""


@dataclass(frozen=True)
class ReplayVersionResult:
    selected_item_ids: dict[str, tuple[str, ...]]
    selected_scores: dict[str, float]


@dataclass(frozen=True)
class SelectedItemsDiff:
    baseline: tuple[str, ...] | None
    candidate: tuple[str, ...] | None


@dataclass(frozen=True)
class ReplayComparison:
    baseline: ReplayVersionResult
    candidate: ReplayVersionResult
    selected_item_diffs: dict[str, SelectedItemsDiff]
    selected_score_deltas: dict[str, float | None]


def load_replay_input(
    trace: RecommendationTrace | dict[str, Any],
) -> RecommendationRunnerInput:
    """Reconstruct the exact privacy-safe ranking inputs stored in a trace."""
    payload = trace.payload if isinstance(trace, RecommendationTrace) else trace
    if payload.get("schema_version") != TRACE_SCHEMA_VERSION:
        raise ReplayError(f"unsupported trace schema version: {payload.get('schema_version')!r}")
    if isinstance(trace, RecommendationTrace) and trace.schema_version != TRACE_SCHEMA_VERSION:
        raise ReplayError(f"unsupported trace row schema version: {trace.schema_version!r}")
    context = _mapping(payload.get("context"), "context")
    wardrobe = _mapping(payload.get("wardrobe"), "wardrobe")
    _mapping(payload.get("memory"), "memory")
    provenance = _mapping(payload.get("provenance"), "provenance")

    raw_items = _list(wardrobe.get("items"), "wardrobe.items")
    items = tuple(_engine_item(_mapping(item, "wardrobe.items[]")) for item in raw_items)
    ranking_signals = _mapping(
        provenance.get("replay_parameters"),
        "provenance.replay_parameters",
    )
    style_profile = dict(
        _mapping(ranking_signals.get("style_profile"), "style_profile")
    )
    fit_profile = dict(_mapping(ranking_signals.get("fit_profile"), "fit_profile"))
    relationships = tuple(
        _relationship(_mapping(edge, "wardrobe.relationships[]"))
        for edge in _list(wardrobe.get("relationships"), "wardrobe.relationships")
    )
    indoor = context.get("indoor_climate")
    seed = provenance.get("deterministic_seed")
    return RecommendationRunnerInput(
        target_date=date.fromisoformat(_string(context.get("target_date"), "target_date")),
        items=items,
        parameters=RecommendationEngineParameters(
            recent_worn_main_item_ids=frozenset(
                _string_list(
                    wardrobe.get("recent_worn_main_item_ids"),
                    "wardrobe.recent_worn_main_item_ids",
                )
            ),
            style_profile=style_profile,
            weather=_weather(context.get("weather")),
            recent_worn_outfit_item_ids=_item_sets(
                wardrobe.get("recent_outfit_item_ids"),
                "wardrobe.recent_outfit_item_ids",
            ),
            fit_profile=fit_profile,
            avoid_exact_repeats=bool(context.get("avoid_exact_repeats", False)),
            disliked_colors=frozenset(
                _string_list(
                    ranking_signals.get("disliked_color_ids"),
                    "provenance.replay_parameters.disliked_color_ids",
                )
            ),
            relationship_edges=relationships,
            suppressed_outfit_item_ids=_item_sets(
                wardrobe.get("suppressed_outfit_item_ids"),
                "wardrobe.suppressed_outfit_item_ids",
            ),
            indoor_climate=(
                IndoorClimateSignal(**_mapping(indoor, "context.indoor_climate"))
                if indoor is not None
                else None
            ),
            context_constraints=tuple(
                _string_list(
                    context.get("normalized_constraints"),
                    "context.normalized_constraints",
                )
            ),
        ),
        deterministic_seed=seed if isinstance(seed, int) else 0,
    )


async def run_replay_input(
    replay_input: RecommendationRunnerInput,
    *,
    engine: RecommendationEngine | None = None,
    parameters: RecommendationEngineParameters | None = None,
) -> EngineResult:
    """Run one deterministic, LLM-free engine replay."""
    try:
        return await run_recommendation_input(
            replay_input,
            engine=engine,
            parameters=parameters,
        )
    except EvaluationError as exc:
        raise ReplayError(str(exc)) from exc


async def replay_exact(
    trace: RecommendationTrace | dict[str, Any],
    *,
    engine: RecommendationEngine | None = None,
) -> EngineResult:
    """Load and run one trace through the current deterministic engine."""
    return await run_replay_input(load_replay_input(trace), engine=engine)


async def compare_versions(
    trace: RecommendationTrace | dict[str, Any],
    *,
    baseline_parameters: RecommendationEngineParameters,
    candidate_parameters: RecommendationEngineParameters,
    baseline_engine: RecommendationEngine | None = None,
    candidate_engine: RecommendationEngine | None = None,
) -> ReplayComparison:
    """Run the same stored input under two parameter/engine variants and diff it."""
    replay_input = load_replay_input(trace)
    baseline_result = await run_replay_input(
        replay_input,
        engine=baseline_engine,
        parameters=baseline_parameters,
    )
    candidate_result = await run_replay_input(
        replay_input,
        engine=candidate_engine,
        parameters=candidate_parameters,
    )
    baseline = _version_result(baseline_result)
    candidate = _version_result(candidate_result)
    outfit_types = sorted(
        set(baseline.selected_item_ids) | set(candidate.selected_item_ids)
    )
    item_diffs = {
        outfit_type: SelectedItemsDiff(
            baseline=baseline.selected_item_ids.get(outfit_type),
            candidate=candidate.selected_item_ids.get(outfit_type),
        )
        for outfit_type in outfit_types
        if baseline.selected_item_ids.get(outfit_type)
        != candidate.selected_item_ids.get(outfit_type)
    }
    score_deltas: dict[str, float | None] = {}
    for outfit_type in outfit_types:
        baseline_score = baseline.selected_scores.get(outfit_type)
        candidate_score = candidate.selected_scores.get(outfit_type)
        score_deltas[outfit_type] = (
            round(candidate_score - baseline_score, 4)
            if baseline_score is not None and candidate_score is not None
            else None
        )
    return ReplayComparison(
        baseline=baseline,
        candidate=candidate,
        selected_item_diffs=item_diffs,
        selected_score_deltas=score_deltas,
    )


def _engine_item(item: dict[str, Any]) -> EngineItem:
    item_id = _string(item.get("id"), "wardrobe.items[].id")
    return EngineItem(
        item_id=item_id,
        title=f"item-{item_id}",
        category=_string(item.get("category"), "wardrobe.items[].category"),
        color_main=_string(item.get("color"), "wardrobe.items[].color"),
        location=item.get("location_bucket")
        if isinstance(item.get("location_bucket"), str)
        else None,
        fit_status=item.get("fit_status")
        if isinstance(item.get("fit_status"), str)
        else "unknown",
        fit_intent=item.get("fit_intent")
        if isinstance(item.get("fit_intent"), str)
        else None,
        sub_category=item.get("subcategory")
        if isinstance(item.get("subcategory"), str)
        else None,
    )


def _relationship(edge: dict[str, Any]) -> RelationshipEdge:
    return RelationshipEdge(
        source_item_id=_string(edge.get("source_item_id"), "relationship.source_item_id"),
        target_item_id=_string(edge.get("target_item_id"), "relationship.target_item_id"),
        relationship_type=_string(edge.get("type"), "relationship.type"),
        score=_number(edge.get("score"), "relationship.score"),
        confidence=_number(edge.get("confidence"), "relationship.confidence"),
    )


def _weather(value: Any) -> WeatherInfo | None:
    if value is None:
        return None
    weather = _mapping(value, "context.weather")
    facts = _mapping(weather.get("facts"), "context.weather.facts")
    return WeatherInfo(
        temperature_c=_number(facts.get("temperature_c"), "weather.temperature_c"),
        feels_like_c=_number(facts.get("feels_like_c"), "weather.feels_like_c"),
        rain_probability=_number(
            facts.get("rain_probability"), "weather.rain_probability"
        ),
        condition=_string(facts.get("condition"), "weather.condition"),
        humidity=facts.get("humidity") if isinstance(facts.get("humidity"), int) else None,
        wind_dir=facts.get("wind_dir") if isinstance(facts.get("wind_dir"), str) else None,
        wind_scale=facts.get("wind_scale")
        if isinstance(facts.get("wind_scale"), str)
        else None,
        wind_speed=float(facts["wind_speed"])
        if isinstance(facts.get("wind_speed"), int | float)
        else None,
        icon=facts.get("icon") if isinstance(facts.get("icon"), str) else None,
        location_name=facts.get("location_name")
        if isinstance(facts.get("location_name"), str)
        else None,
    )


def _version_result(result: EngineResult) -> ReplayVersionResult:
    return ReplayVersionResult(
        selected_item_ids={
            outfit.outfit_type: tuple(outfit.item_ids) for outfit in result.outfits
        },
        selected_scores={
            outfit.outfit_type: outfit.score_total for outfit in result.outfits
        },
    )


def _mapping(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ReplayError(f"{field} must be an object")
    return cast(dict[str, Any], value)


def _list(value: Any, field: str) -> list[Any]:
    if not isinstance(value, list):
        raise ReplayError(f"{field} must be an array")
    return cast(list[Any], value)


def _string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReplayError(f"{field} must be a non-empty string")
    return value


def _string_list(value: Any, field: str) -> list[str]:
    raw = _list(value, field)
    if not all(isinstance(item, str) for item in raw):
        raise ReplayError(f"{field} must contain only strings")
    return cast(list[str], raw)


def _item_sets(value: Any, field: str) -> tuple[frozenset[str], ...]:
    return tuple(
        frozenset(_string_list(item_ids, f"{field}[]"))
        for item_ids in _list(value, field)
    )


def _number(value: Any, field: str) -> float:
    if not isinstance(value, int | float) or isinstance(value, bool):
        raise ReplayError(f"{field} must be numeric")
    return float(value)
