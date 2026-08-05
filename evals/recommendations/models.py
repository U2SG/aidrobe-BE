from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal, Self
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

CategoryName = Literal["top", "bottom", "outerwear", "dress", "shoes"]
Availability = Literal["available", "laundry", "unavailable"]
ContextConstraint = Literal[
    "exclude_dress",
    "formal",
    "comfort",
    "removable_layer",
    "low_key",
    "reduce_weather",
]
HardInvariant = Literal[
    "status_ready",
    "slot_completeness",
    "available_items_only",
    "weather_suitable",
    "focused_item_included",
    "no_exact_recent_repeat",
    "reason_grounded",
    "outcome_constraints",
    "forecast_unavailable_honest",
    "indoor_climate_applied",
]

SCENARIO_SCHEMA_DIALECT = "https://json-schema.org/draft/2020-12/schema"
SCENARIO_SCHEMA_ID = (
    "https://github.com/U2SG/aidrobe-BE/evals/recommendations/schema.v1.json"
)


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class UserSettingsFixture(StrictModel):
    timezone: str
    location: str
    commute_exposure: Literal["less_outdoor", "more_outdoor", "unknown"] | None = None
    preferences: dict[str, Any] = Field(default_factory=dict)

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown IANA timezone: {value}") from exc
        return value


class WardrobeItemFixture(StrictModel):
    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    category: CategoryName
    color_main: str = Field(min_length=1)
    availability: Availability = "available"
    location: str | None = None
    fit_status: str = "unknown"
    fit_intent: str | None = None
    sub_category: str | None = None


class RecentOutfitFixture(StrictModel):
    worn_at: datetime
    item_ids: list[str] = Field(min_length=1)


class TemperatureMismatchFixture(StrictModel):
    reported_at: datetime
    reason: Literal["too_cold", "too_warm"]
    signal_strength: Literal["weak", "medium", "strong"] = "strong"


class WearHistoryFixture(StrictModel):
    recent_main_item_ids: list[str] = Field(default_factory=list)
    recent_outfits: list[RecentOutfitFixture] = Field(default_factory=list)
    temperature_mismatch_events: list[TemperatureMismatchFixture] = Field(default_factory=list)


class RelationshipEdgeFixture(StrictModel):
    source_item_id: str
    target_item_id: str
    relationship_type: str
    score: float
    confidence: float = Field(ge=0.0, le=1.0)


class LearnedSignalsFixture(StrictModel):
    style_profile: dict[str, Any] = Field(default_factory=dict)
    fit_profile: dict[str, Any] = Field(default_factory=dict)
    disliked_colors: list[str] = Field(default_factory=list)
    relationship_edges: list[RelationshipEdgeFixture] = Field(default_factory=list)
    suppressed_outfits: list[list[str]] = Field(default_factory=list)
    avoid_exact_repeats: bool = False


class WeatherSnapshotFixture(StrictModel):
    temperature_c: float
    feels_like_c: float
    rain_probability: float = Field(ge=0.0, le=1.0)
    condition: str = Field(min_length=1)
    humidity: int | None = Field(default=None, ge=0, le=100)
    wind_dir: str | None = None
    wind_scale: str | None = None
    wind_speed: float | None = None
    icon: str | None = None
    location_name: str | None = None


class WeatherFixture(StrictModel):
    current: WeatherSnapshotFixture | None = None
    forecast_by_date: dict[date, WeatherSnapshotFixture | None] = Field(default_factory=dict)


class RecommendationRequestFixture(StrictModel):
    target_date: date
    scene: str = Field(min_length=1)
    active_constraints: list[ContextConstraint] = Field(default_factory=list)
    focused_item_id: str | None = None


class SelectedOutfitExpectation(StrictModel):
    required_item_ids: list[str] = Field(default_factory=list)
    allowed_item_ids: list[str] = Field(default_factory=list)
    forbidden_item_ids: list[str] = Field(default_factory=list)
    allowed_categories: list[CategoryName] = Field(default_factory=list)
    forbidden_categories: list[CategoryName] = Field(default_factory=list)


class ScenarioExpectation(StrictModel):
    hard_invariants: list[HardInvariant] = Field(min_length=1)
    acceptable_slot_sets: list[list[CategoryName]] = Field(min_length=1)
    selected_outfit: SelectedOutfitExpectation = Field(default_factory=SelectedOutfitExpectation)
    expected_reason_sources: list[str] = Field(default_factory=list)


class RecommendationScenario(StrictModel):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9-]*$")
    description: str = Field(min_length=1)
    user_settings: UserSettingsFixture
    wardrobe: list[WardrobeItemFixture] = Field(min_length=1)
    wear_history: WearHistoryFixture
    learned_signals: LearnedSignalsFixture
    weather: WeatherFixture
    request: RecommendationRequestFixture
    expected: ScenarioExpectation

    @model_validator(mode="after")
    def validate_item_references(self) -> Self:
        item_ids = [item.id for item in self.wardrobe]
        known = set(item_ids)
        if len(item_ids) != len(known):
            raise ValueError("wardrobe item ids must be unique")

        references: list[tuple[str, str]] = []
        references.extend(
            ("wear_history.recent_main_item_ids", item_id)
            for item_id in self.wear_history.recent_main_item_ids
        )
        for recent in self.wear_history.recent_outfits:
            references.extend(("wear_history.recent_outfits", item_id) for item_id in recent.item_ids)
        for suppressed in self.learned_signals.suppressed_outfits:
            references.extend(("learned_signals.suppressed_outfits", item_id) for item_id in suppressed)
        for edge in self.learned_signals.relationship_edges:
            references.append(("learned_signals.relationship_edges", edge.source_item_id))
            references.append(("learned_signals.relationship_edges", edge.target_item_id))
        if self.request.focused_item_id is not None:
            references.append(("request.focused_item_id", self.request.focused_item_id))
        outcome = self.expected.selected_outfit
        for field_name in ("required_item_ids", "allowed_item_ids", "forbidden_item_ids"):
            references.extend(
                (f"expected.selected_outfit.{field_name}", item_id)
                for item_id in getattr(outcome, field_name)
            )

        unknown = [(field, item_id) for field, item_id in references if item_id not in known]
        if unknown:
            raise ValueError(f"unknown wardrobe item reference(s): {unknown}")
        if (
            "focused_item_included" in self.expected.hard_invariants
            and self.request.focused_item_id is None
        ):
            raise ValueError("focused_item_included requires request.focused_item_id")
        return self


class RecommendationCorpus(StrictModel):
    schema_version: Literal["1.0"]
    fixed_clock: datetime
    random_seed: int
    scenarios: list[RecommendationScenario] = Field(min_length=12)

    @model_validator(mode="after")
    def validate_scenario_ids(self) -> Self:
        scenario_ids = [scenario.id for scenario in self.scenarios]
        if len(scenario_ids) != len(set(scenario_ids)):
            raise ValueError("scenario ids must be unique")
        return self


def recommendation_corpus_json_schema() -> dict[str, Any]:
    """Return the canonical committed schema generated from the runtime contract."""
    schema = RecommendationCorpus.model_json_schema()
    schema["$schema"] = SCENARIO_SCHEMA_DIALECT
    schema["$id"] = SCENARIO_SCHEMA_ID
    return schema


class SlotCompletenessMetric(StrictModel):
    passed: bool
    selected_categories: list[str]
    acceptable_slot_sets: list[list[str]]
    missing_by_slot_set: list[list[str]]


class AvailabilityMetric(StrictModel):
    passed: bool
    violation_item_ids: list[str]


class WeatherSuitabilityMetric(StrictModel):
    passed: bool
    weather_available: bool
    violations: list[str]


class FocusedItemMetric(StrictModel):
    required: bool
    focused_item_id: str | None
    included: bool | None


class NoveltyMetric(StrictModel):
    exact_recent_repeat: bool
    max_recent_similarity: float
    novelty: float


class ReasonGroundingMetric(StrictModel):
    passed: bool
    referenced_item_ids: list[str]
    invalid_snapshot_item_ids: list[str]
    invalid_outfit_item_ids: list[str]
    malformed_entries: list[str]
    present_sources: list[str]
    missing_expected_sources: list[str]


class OutcomeConstraintsMetric(StrictModel):
    passed: bool
    violations: list[str]


class SelectedCandidateMetric(StrictModel):
    outfit_type: str
    item_ids: list[str]
    score: float
    rank_within_outfit_type: int = Field(ge=1)
    candidate_count_within_outfit_type: int = Field(ge=1)


class ScenarioMetrics(StrictModel):
    hard_constraint_satisfaction: bool
    hard_invariant_checks: dict[str, bool]
    outfit_slot_completeness: SlotCompletenessMetric
    unavailable_or_laundry: AvailabilityMetric
    weather_suitability: WeatherSuitabilityMetric
    focused_item_inclusion: FocusedItemMetric
    recent_combination: NoveltyMetric
    reason_grounding: ReasonGroundingMetric
    outcome_constraints: OutcomeConstraintsMetric
    forecast_unavailable_honest: bool
    indoor_climate_applied: bool
    selected_candidate: SelectedCandidateMetric | None


class ScenarioReport(StrictModel):
    id: str
    description: str
    passed: bool
    invariant_failures: list[str]
    metrics: ScenarioMetrics


class ReportSummary(StrictModel):
    scenario_count: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    hard_invariant_failures: int = Field(ge=0)


class EvaluationReport(StrictModel):
    report_version: Literal["1.0"]
    scenario_schema_version: Literal["1.0"]
    generated_at: datetime
    random_seed: int
    summary: ReportSummary
    scenarios: list[ScenarioReport]
