from __future__ import annotations

import argparse
import asyncio
import json
import random
import sys
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import ValidationError

from app.application.ports.weather import WeatherInfo
from app.application.recommendations.engine import (
    EngineItem,
    EngineOutfit,
    EngineResult,
    RecommendationEngine,
    RelationshipEdge,
)
from app.application.recommendations.indoor_climate import IndoorClimateSignal, infer_indoor_climate
from app.common.enums import Category, RecommendationEventType
from evals.recommendations.models import (
    AvailabilityMetric,
    EvaluationReport,
    FocusedItemMetric,
    NoveltyMetric,
    OutcomeConstraintsMetric,
    ReasonGroundingMetric,
    RecommendationCorpus,
    RecommendationScenario,
    ReportSummary,
    ScenarioMetrics,
    ScenarioReport,
    SelectedCandidateMetric,
    SlotCompletenessMetric,
    WeatherSnapshotFixture,
    WeatherSuitabilityMetric,
    recommendation_corpus_json_schema,
)

PACKAGE_DIR = Path(__file__).resolve().parent
DEFAULT_CORPUS_PATH = PACKAGE_DIR / "scenarios.v1.json"
DEFAULT_SCHEMA_PATH = PACKAGE_DIR / "schema.v1.json"
DEFAULT_OUTPUT_DIR = PACKAGE_DIR.parent / "reports"
JSON_REPORT_NAME = "recommendation-eval.json"
MARKDOWN_REPORT_NAME = "recommendation-eval.md"

_LIGHT_SHOE_COLORS = {
    "white",
    "ivory",
    "cream",
    "beige",
    "light gray",
    "light grey",
    "silver",
    "pink",
    "yellow",
}


class EvaluationError(RuntimeError):
    """The corpus, schema, or generated report is malformed."""


@dataclass(frozen=True)
class FixedClock:
    now: datetime


@dataclass(frozen=True)
class RecommendationEngineParameters:
    """Deterministic engine parameters shared by corpus and trace replays."""

    recent_worn_main_item_ids: frozenset[str]
    style_profile: dict[str, Any]
    weather: WeatherInfo | None
    recent_worn_outfit_item_ids: tuple[frozenset[str], ...]
    fit_profile: dict[str, Any]
    avoid_exact_repeats: bool
    disliked_colors: frozenset[str]
    relationship_edges: tuple[RelationshipEdge, ...]
    suppressed_outfit_item_ids: tuple[frozenset[str], ...]
    indoor_climate: IndoorClimateSignal | None
    context_constraints: tuple[str, ...]


@dataclass(frozen=True)
class RecommendationRunnerInput:
    """Fully resolved input accepted by the deterministic eval runner."""

    target_date: date
    items: tuple[EngineItem, ...]
    parameters: RecommendationEngineParameters
    deterministic_seed: int | None = None


@dataclass(frozen=True)
class TemperatureMismatchEvent:
    event_type: str
    event_metadata: dict[str, Any] | None
    signal_strength: str | None
    created_at: datetime


@dataclass(frozen=True)
class InMemoryScenarioRepository:
    """Persistence boundary for the eval: scenario data only, never a database."""

    scenario: RecommendationScenario

    def wearable_items(self) -> list[EngineItem]:
        return [
            EngineItem(
                item_id=item.id,
                title=item.title,
                category=item.category,
                color_main=item.color_main,
                location=item.location,
                fit_status=item.fit_status,
                fit_intent=item.fit_intent,
                sub_category=item.sub_category,
            )
            for item in self.scenario.wardrobe
            if item.availability == "available"
        ]

    def recent_main_item_ids(self) -> set[str]:
        return set(self.scenario.wear_history.recent_main_item_ids)

    def recent_outfit_item_ids(self) -> list[set[str]]:
        return [set(outfit.item_ids) for outfit in self.scenario.wear_history.recent_outfits]

    def temperature_mismatch_events(self) -> list[TemperatureMismatchEvent]:
        event_type = RecommendationEventType.TEMPERATURE_MISMATCH_REPORTED.value
        return [
            TemperatureMismatchEvent(
                event_type=event_type,
                event_metadata={"reason": event.reason},
                signal_strength=event.signal_strength,
                created_at=event.reported_at,
            )
            for event in self.scenario.wear_history.temperature_mismatch_events
        ]


class FakeWeatherAdapter:
    """Recorded weather port. It has no code path capable of network access."""

    def __init__(self, scenario: RecommendationScenario) -> None:
        self._weather = scenario.weather

    async def get_weather(self, city: str) -> WeatherInfo:
        del city
        if self._weather.current is None:
            raise LookupError("no recorded current weather")
        return _to_weather_info(self._weather.current)

    async def get_today(self, location: str) -> WeatherInfo:
        return await self.get_weather(location)

    async def get_forecast(self, location: str, target_date: date) -> WeatherInfo | None:
        del location
        snapshot = self._weather.forecast_by_date.get(target_date)
        return _to_weather_info(snapshot) if snapshot is not None else None


class EvaluationRecommendationEngine(RecommendationEngine):
    """Observes final candidates while leaving production selection unchanged."""

    def __init__(self) -> None:
        super().__init__(llm=None)
        self.ranked_candidates: list[EngineOutfit] = []

    def _apply_suppression(
        self,
        candidates: list[EngineOutfit],
        suppressed_outfit_item_ids: list[set[str]],
    ) -> list[EngineOutfit]:
        ranked = super()._apply_suppression(candidates, suppressed_outfit_item_ids)
        self.ranked_candidates = ranked
        return ranked


async def run_recommendation_input(
    runner_input: RecommendationRunnerInput,
    *,
    engine: RecommendationEngine | None = None,
    parameters: RecommendationEngineParameters | None = None,
) -> EngineResult:
    """Run a fully resolved input through the real engine with LLM disabled."""
    active_engine = engine or RecommendationEngine(llm=None)
    if active_engine.llm is not None:
        raise EvaluationError("recommendation eval engines must set llm=None")
    active_parameters = parameters or runner_input.parameters
    if runner_input.deterministic_seed is not None:
        random.seed(runner_input.deterministic_seed)
    return await active_engine.generate(
        list(runner_input.items),
        recent_worn_main_item_ids=set(active_parameters.recent_worn_main_item_ids),
        target_date=runner_input.target_date,
        style_profile=active_parameters.style_profile,
        weather=active_parameters.weather,
        recent_worn_outfit_item_ids=[
            set(item_ids) for item_ids in active_parameters.recent_worn_outfit_item_ids
        ],
        fit_profile=active_parameters.fit_profile,
        avoid_exact_repeats=active_parameters.avoid_exact_repeats,
        disliked_colors=set(active_parameters.disliked_colors),
        relationship_edges=list(active_parameters.relationship_edges),
        suppressed_outfit_item_ids=[
            set(item_ids) for item_ids in active_parameters.suppressed_outfit_item_ids
        ],
        indoor_climate=active_parameters.indoor_climate,
        context_constraints=list(active_parameters.context_constraints),
    )


def _to_weather_info(snapshot: WeatherSnapshotFixture) -> WeatherInfo:
    return WeatherInfo(**snapshot.model_dump())


def load_corpus(path: Path) -> RecommendationCorpus:
    try:
        schema = json.loads(DEFAULT_SCHEMA_PATH.read_text(encoding="utf-8"))
        if schema != recommendation_corpus_json_schema():
            raise EvaluationError(
                f"{DEFAULT_SCHEMA_PATH} is malformed or has drifted from the Pydantic contract"
            )
        return RecommendationCorpus.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValidationError) as exc:
        raise EvaluationError(f"invalid recommendation eval corpus or schema: {exc}") from exc


async def _resolve_weather(
    scenario: RecommendationScenario,
    adapter: FakeWeatherAdapter,
    clock: FixedClock,
) -> WeatherInfo | None:
    timezone = ZoneInfo(scenario.user_settings.timezone)
    local_today = clock.now.astimezone(timezone).date()
    target_date = scenario.request.target_date
    if target_date > local_today:
        return await adapter.get_forecast(scenario.user_settings.location, target_date)
    forecast = await adapter.get_forecast(scenario.user_settings.location, target_date)
    if forecast is not None:
        return forecast
    try:
        return await adapter.get_weather(scenario.user_settings.location)
    except LookupError:
        return None


def _derive_indoor_climate(
    repository: InMemoryScenarioRepository,
    clock: FixedClock,
) -> IndoorClimateSignal | None:
    return infer_indoor_climate(
        repository.temperature_mismatch_events(),
        repository.scenario.user_settings.commute_exposure,
        now=clock.now,
    )


async def evaluate_scenario(
    scenario: RecommendationScenario,
    clock: FixedClock,
) -> ScenarioReport:
    repository = InMemoryScenarioRepository(scenario)
    weather = await _resolve_weather(scenario, FakeWeatherAdapter(scenario), clock)
    indoor_climate = _derive_indoor_climate(repository, clock)
    learned = scenario.learned_signals
    engine = EvaluationRecommendationEngine()
    runner_input = RecommendationRunnerInput(
        target_date=scenario.request.target_date,
        items=tuple(repository.wearable_items()),
        parameters=RecommendationEngineParameters(
            recent_worn_main_item_ids=frozenset(repository.recent_main_item_ids()),
            style_profile=learned.style_profile,
            weather=weather,
            recent_worn_outfit_item_ids=tuple(
                frozenset(item_ids) for item_ids in repository.recent_outfit_item_ids()
            ),
            fit_profile=learned.fit_profile,
            avoid_exact_repeats=learned.avoid_exact_repeats,
            disliked_colors=frozenset(color.lower() for color in learned.disliked_colors),
            relationship_edges=tuple(
                RelationshipEdge(
                    source_item_id=edge.source_item_id,
                    target_item_id=edge.target_item_id,
                    relationship_type=edge.relationship_type,
                    score=edge.score,
                    confidence=edge.confidence,
                )
                for edge in learned.relationship_edges
            ),
            suppressed_outfit_item_ids=tuple(
                frozenset(item_ids) for item_ids in learned.suppressed_outfits
            ),
            indoor_climate=indoor_climate,
            context_constraints=tuple(scenario.request.active_constraints),
        ),
    )
    result = await run_recommendation_input(runner_input, engine=engine)

    primary = result.outfits[0] if result.outfits else None
    item_by_id = {item.id: item for item in scenario.wardrobe}
    slot_metric = _slot_completeness(scenario, primary, item_by_id)
    availability_metric = _availability(scenario, result.outfits, item_by_id)
    weather_metric = _weather_suitability(result.outfits, item_by_id, weather)
    focus_metric = _focused_item(scenario, primary)
    novelty_metric = _recent_combination(primary, repository.recent_outfit_item_ids())
    reason_metric = _reason_grounding(scenario, result.outfits)
    outcome_metric = _outcome_constraints(scenario, primary, item_by_id)
    forecast_honest = _forecast_unavailable_honest(scenario, clock, weather, reason_metric)
    indoor_applied = _indoor_climate_applied(primary, item_by_id, indoor_climate, reason_metric)
    selected_candidate = _selected_candidate(primary, engine.ranked_candidates)

    all_checks = {
        "status_ready": result.status == "ready" and primary is not None,
        "slot_completeness": slot_metric.passed,
        "available_items_only": availability_metric.passed,
        "weather_suitable": weather_metric.passed,
        "focused_item_included": focus_metric.included is True,
        "no_exact_recent_repeat": not novelty_metric.exact_recent_repeat,
        "reason_grounded": reason_metric.passed,
        "outcome_constraints": outcome_metric.passed,
        "forecast_unavailable_honest": forecast_honest,
        "indoor_climate_applied": indoor_applied,
    }
    hard_checks = {
        invariant: all_checks[invariant]
        for invariant in scenario.expected.hard_invariants
    }
    failures = [invariant for invariant, passed in hard_checks.items() if not passed]
    metrics = ScenarioMetrics(
        hard_constraint_satisfaction=not failures,
        hard_invariant_checks=hard_checks,
        outfit_slot_completeness=slot_metric,
        unavailable_or_laundry=availability_metric,
        weather_suitability=weather_metric,
        focused_item_inclusion=focus_metric,
        recent_combination=novelty_metric,
        reason_grounding=reason_metric,
        outcome_constraints=outcome_metric,
        forecast_unavailable_honest=forecast_honest,
        indoor_climate_applied=indoor_applied,
        selected_candidate=selected_candidate,
    )
    return ScenarioReport(
        id=scenario.id,
        description=scenario.description,
        passed=not failures,
        invariant_failures=failures,
        metrics=metrics,
    )


def _slot_completeness(
    scenario: RecommendationScenario,
    primary: EngineOutfit | None,
    item_by_id: dict[str, Any],
) -> SlotCompletenessMetric:
    categories = [item_by_id[item_id].category for item_id in primary.item_ids] if primary else []
    category_set = set(categories)
    acceptable = [list(slots) for slots in scenario.expected.acceptable_slot_sets]
    missing = [sorted(set(slots) - category_set) for slots in acceptable]
    return SlotCompletenessMetric(
        passed=primary is not None and any(not missing_slots for missing_slots in missing),
        selected_categories=categories,
        acceptable_slot_sets=acceptable,
        missing_by_slot_set=missing,
    )


def _availability(
    scenario: RecommendationScenario,
    outfits: list[EngineOutfit],
    item_by_id: dict[str, Any],
) -> AvailabilityMetric:
    violations = sorted(
        {
            item_id
            for outfit in outfits
            for item_id in outfit.item_ids
            if item_by_id[item_id].availability != "available"
        }
    )
    return AvailabilityMetric(passed=not violations, violation_item_ids=violations)


def _weather_suitability(
    outfits: list[EngineOutfit],
    item_by_id: dict[str, Any],
    weather: WeatherInfo | None,
) -> WeatherSuitabilityMetric:
    violations: list[str] = []
    if weather is not None:
        for rank, outfit in enumerate(outfits, start=1):
            items = [item_by_id[item_id] for item_id in outfit.item_ids]
            categories = {item.category for item in items}
            if weather.temperature_c < 10 and Category.OUTERWEAR.value not in categories:
                violations.append(f"outfit {rank}: cold_without_outerwear")
            if weather.rain_probability > 0.5 and any(
                item.category == Category.SHOES.value
                and item.color_main.lower() in _LIGHT_SHOE_COLORS
                for item in items
            ):
                violations.append(f"outfit {rank}: rain_with_light_shoes")
    return WeatherSuitabilityMetric(
        passed=not violations,
        weather_available=weather is not None,
        violations=violations,
    )


def _focused_item(
    scenario: RecommendationScenario,
    primary: EngineOutfit | None,
) -> FocusedItemMetric:
    focused_item_id = scenario.request.focused_item_id
    included = None
    if focused_item_id is not None:
        included = primary is not None and focused_item_id in primary.item_ids
    return FocusedItemMetric(
        required=focused_item_id is not None,
        focused_item_id=focused_item_id,
        included=included,
    )


def _recent_combination(
    primary: EngineOutfit | None,
    recent_outfits: list[set[str]],
) -> NoveltyMetric:
    current = set(primary.item_ids) if primary else set()
    similarities = [_jaccard(current, recent) for recent in recent_outfits]
    max_similarity = max(similarities, default=0.0)
    exact_repeat = bool(current) and any(current == recent for recent in recent_outfits)
    return NoveltyMetric(
        exact_recent_repeat=exact_repeat,
        max_recent_similarity=round(max_similarity, 4),
        novelty=round(1.0 - max_similarity, 4),
    )


def _jaccard(left: set[str], right: set[str]) -> float:
    union = left | right
    return len(left & right) / len(union) if union else 0.0


def _reason_grounding(
    scenario: RecommendationScenario,
    outfits: list[EngineOutfit],
) -> ReasonGroundingMetric:
    known_item_ids = {item.id for item in scenario.wardrobe}
    referenced: set[str] = set()
    invalid_snapshot: set[str] = set()
    invalid_outfit: set[str] = set()
    sources: set[str] = set()
    malformed: list[str] = []
    for outfit_index, outfit in enumerate(outfits, start=1):
        outfit_ids = set(outfit.item_ids)
        for reason_index, reason in enumerate(outfit.reasoning, start=1):
            if not isinstance(reason, dict):
                malformed.append(f"outfit {outfit_index} reason {reason_index}: expected object")
                continue
            source = reason.get("source")
            text = reason.get("text")
            reason_item_ids = reason.get("referenced_item_ids")
            if not isinstance(source, str) or not source or not isinstance(text, str) or not text:
                malformed.append(f"outfit {outfit_index} reason {reason_index}: missing source/text")
            else:
                sources.add(source)
            if not isinstance(reason_item_ids, list) or not all(
                isinstance(item_id, str) for item_id in reason_item_ids
            ):
                malformed.append(f"outfit {outfit_index} reason {reason_index}: invalid referenced_item_ids")
                continue
            referenced.update(reason_item_ids)
            invalid_snapshot.update(set(reason_item_ids) - known_item_ids)
            invalid_outfit.update(set(reason_item_ids) - outfit_ids)
    missing_sources = sorted(set(scenario.expected.expected_reason_sources) - sources)
    passed = (
        bool(outfits)
        and bool(sources)
        and not invalid_snapshot
        and not invalid_outfit
        and not missing_sources
        and not malformed
    )
    return ReasonGroundingMetric(
        passed=passed,
        referenced_item_ids=sorted(referenced),
        invalid_snapshot_item_ids=sorted(invalid_snapshot),
        invalid_outfit_item_ids=sorted(invalid_outfit),
        malformed_entries=malformed,
        present_sources=sorted(sources),
        missing_expected_sources=missing_sources,
    )


def _outcome_constraints(
    scenario: RecommendationScenario,
    primary: EngineOutfit | None,
    item_by_id: dict[str, Any],
) -> OutcomeConstraintsMetric:
    expected = scenario.expected.selected_outfit
    if primary is None:
        return OutcomeConstraintsMetric(passed=False, violations=["no selected outfit"])
    selected_ids = set(primary.item_ids)
    selected_categories = {item_by_id[item_id].category for item_id in primary.item_ids}
    violations: list[str] = []
    missing_required = sorted(set(expected.required_item_ids) - selected_ids)
    if missing_required:
        violations.append(f"missing required items: {missing_required}")
    if expected.allowed_item_ids:
        disallowed = sorted(selected_ids - set(expected.allowed_item_ids))
        if disallowed:
            violations.append(f"items outside allowed set: {disallowed}")
    forbidden_items = sorted(selected_ids & set(expected.forbidden_item_ids))
    if forbidden_items:
        violations.append(f"forbidden items selected: {forbidden_items}")
    if expected.allowed_categories:
        disallowed_categories = sorted(selected_categories - set(expected.allowed_categories))
        if disallowed_categories:
            violations.append(f"categories outside allowed set: {disallowed_categories}")
    forbidden_categories = sorted(selected_categories & set(expected.forbidden_categories))
    if forbidden_categories:
        violations.append(f"forbidden categories selected: {forbidden_categories}")
    return OutcomeConstraintsMetric(passed=not violations, violations=violations)


def _forecast_unavailable_honest(
    scenario: RecommendationScenario,
    clock: FixedClock,
    weather: WeatherInfo | None,
    reason_metric: ReasonGroundingMetric,
) -> bool:
    local_today = clock.now.astimezone(ZoneInfo(scenario.user_settings.timezone)).date()
    target_is_future = scenario.request.target_date > local_today
    target_forecast = scenario.weather.forecast_by_date.get(scenario.request.target_date)
    forecast_unavailable = target_is_future and target_forecast is None
    if not forecast_unavailable:
        return True
    return weather is None and "weather_rule" not in reason_metric.present_sources


def _indoor_climate_applied(
    primary: EngineOutfit | None,
    item_by_id: dict[str, Any],
    indoor_climate: IndoorClimateSignal | None,
    reason_metric: ReasonGroundingMetric,
) -> bool:
    if primary is None or indoor_climate is None:
        return False
    has_outerwear = any(
        item_by_id[item_id].category == Category.OUTERWEAR.value
        for item_id in primary.item_ids
    )
    return has_outerwear and "indoor_climate_inference" in reason_metric.present_sources


def _selected_candidate(
    primary: EngineOutfit | None,
    candidates: list[EngineOutfit],
) -> SelectedCandidateMetric | None:
    if primary is None:
        return None
    pool = [candidate for candidate in candidates if candidate.outfit_type == primary.outfit_type]
    ordered = sorted(enumerate(pool), key=lambda pair: (-pair[1].score_total, pair[0]))
    for rank, (_, candidate) in enumerate(ordered, start=1):
        if candidate.item_ids == primary.item_ids and candidate.score_total == primary.score_total:
            return SelectedCandidateMetric(
                outfit_type=primary.outfit_type,
                item_ids=list(primary.item_ids),
                score=primary.score_total,
                rank_within_outfit_type=rank,
                candidate_count_within_outfit_type=len(pool),
            )
    raise EvaluationError("selected outfit was not found in the captured candidate set")


async def evaluate_corpus(corpus: RecommendationCorpus) -> EvaluationReport:
    random.seed(corpus.random_seed)
    clock = FixedClock(corpus.fixed_clock)
    scenarios = [await evaluate_scenario(scenario, clock) for scenario in corpus.scenarios]
    failed = [scenario for scenario in scenarios if not scenario.passed]
    failure_count = sum(len(scenario.invariant_failures) for scenario in scenarios)
    return EvaluationReport(
        report_version="1.0",
        scenario_schema_version=corpus.schema_version,
        generated_at=clock.now,
        random_seed=corpus.random_seed,
        summary=ReportSummary(
            scenario_count=len(scenarios),
            passed=len(scenarios) - len(failed),
            failed=len(failed),
            hard_invariant_failures=failure_count,
        ),
        scenarios=scenarios,
    )


def write_reports(report: EvaluationReport, output_dir: Path) -> tuple[Path, Path]:
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        json_path = output_dir / JSON_REPORT_NAME
        markdown_path = output_dir / MARKDOWN_REPORT_NAME
        payload = report.model_dump(mode="json")
        json_text = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        json_path.write_text(json_text, encoding="utf-8", newline="\n")
        markdown_path.write_text(_markdown_report(report), encoding="utf-8", newline="\n")
        reloaded = EvaluationReport.model_validate_json(json_path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        raise EvaluationError(f"malformed recommendation eval report: {exc}") from exc
    if reloaded != report:
        raise EvaluationError("serialized recommendation eval report did not round-trip")
    return json_path, markdown_path


def _markdown_report(report: EvaluationReport) -> str:
    lines = [
        "# Recommendation Evaluation Report",
        "",
        f"- Scenario schema: `{report.scenario_schema_version}`",
        f"- Fixed clock: `{report.generated_at.isoformat()}`",
        f"- Fixed random seed: `{report.random_seed}`",
        f"- Result: {report.summary.passed}/{report.summary.scenario_count} scenarios passed",
        f"- Hard-invariant failures: {report.summary.hard_invariant_failures}",
        "",
        "| Scenario | Result | Failed invariants | Score | Candidate rank | Selected items |",
        "|---|---|---|---:|---:|---|",
    ]
    for scenario in report.scenarios:
        selected = scenario.metrics.selected_candidate
        failures = ", ".join(scenario.invariant_failures) or "-"
        score = f"{selected.score:.4f}" if selected else "-"
        rank = (
            f"{selected.rank_within_outfit_type}/{selected.candidate_count_within_outfit_type}"
            if selected
            else "-"
        )
        items = ", ".join(selected.item_ids) if selected else "-"
        result = "PASS" if scenario.passed else "FAIL"
        lines.append(f"| `{scenario.id}` | {result} | {failures} | {score} | {rank} | {items} |")

    for scenario in report.scenarios:
        metrics = scenario.metrics
        lines.extend(
            [
                "",
                f"## {scenario.id}",
                "",
                scenario.description,
                "",
                f"- Hard constraints: {'PASS' if metrics.hard_constraint_satisfaction else 'FAIL'}",
                f"- Slot completeness: {'PASS' if metrics.outfit_slot_completeness.passed else 'FAIL'}",
                f"- Availability/laundry: {'PASS' if metrics.unavailable_or_laundry.passed else 'FAIL'}",
                f"- Weather suitability: {'PASS' if metrics.weather_suitability.passed else 'FAIL'}",
                f"- Focused item required: {metrics.focused_item_inclusion.required}",
                f"- Exact recent repeat: {metrics.recent_combination.exact_recent_repeat}",
                f"- Novelty: {metrics.recent_combination.novelty:.4f}",
                f"- Reason grounding: {'PASS' if metrics.reason_grounding.passed else 'FAIL'}",
                f"- Reason sources: {', '.join(metrics.reason_grounding.present_sources) or '-'}",
            ]
        )
    return "\n".join(lines) + "\n"


async def run(corpus_path: Path, output_dir: Path) -> tuple[EvaluationReport, Path, Path]:
    corpus = load_corpus(corpus_path)
    report = await evaluate_corpus(corpus)
    json_path, markdown_path = write_reports(report, output_dir)
    return report, json_path, markdown_path


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run deterministic recommendation evaluations")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS_PATH)
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        report, json_path, markdown_path = asyncio.run(run(args.corpus, args.output_dir))
    except EvaluationError as exc:
        print(f"recommendation eval malformed: {exc}", file=sys.stderr)
        return 2
    print(
        f"recommendation eval: {report.summary.passed}/{report.summary.scenario_count} passed; "
        f"reports: {json_path}, {markdown_path}"
    )
    return 1 if report.summary.hard_invariant_failures else 0
