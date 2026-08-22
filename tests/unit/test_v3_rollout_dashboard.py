"""V3-023 — the rollout dashboard is computable from the frozen event contract.

These tests are the teeth of the feature. The dashboard's claim is that every
V3_SPEC §20 kill criterion can be computed from events this system actually
emits, so the chain is asserted link by link:

    kill criterion -> metric -> event field -> event -> emitter on the owning side

The one link deliberately NOT asserted is the sink, because choosing one is a
human gate — ``test_the_sink_gap_is_declared_unmet`` pins that honesty instead.

The FE half is ``scripts/v3RolloutDashboard.test.ts`` in aidrobe-FE, which
asserts the same artifact byte-for-byte and covers the 7 fe-owned emitters.
"""

import json
import re
from pathlib import Path

from app.common.analytics_events import (
    ENVELOPE_FIELDS,
    FORBIDDEN_FIELD_NAMES,
    KILL_CRITERIA_COUNT,
    QUALITATIVE_ONLY_KILL_CRITERIA,
    V3_ANALYTICS_EVENTS,
    V3_ANALYTICS_METRICS,
    V3AnalyticsEventName,
)
from app.common.rollout_dashboard import (
    CONTRACT_PATH,
    COST_MODEL,
    OPEN_HUMAN_GATES,
    PIPELINE_PREREQUISITES,
    READINESS_GATES,
    REQUIRED_DIMENSIONS,
    SEED_STUDY_QUESTIONS,
    V3_ROLLOUT_PANELS,
    ReadinessGroup,
    ReadinessStatus,
    RolloutDimension,
    contract_dict,
    kill_criteria_coverage,
)

APP_ROOT = Path(__file__).resolve().parents[2] / "app"


def _fields_reachable_from(events: tuple[str, ...]) -> set[str]:
    """Envelope fields plus every field carried by AT LEAST ONE of ``events``.

    The union (not the intersection) is the correct rule: a metric legitimately
    joins events that carry different fields — ``consent_prompt_completion_delta``
    reads ``context`` off ``v3_avatar_prompt_shown`` while joining decision
    events that have no such field.
    """
    reachable = set(ENVELOPE_FIELDS)
    for event in events:
        reachable |= set(V3_ANALYTICS_EVENTS[V3AnalyticsEventName(event)].fields)
    return reachable


# --- the artifact is generated, not hand-edited -------------------------------


def test_json_artifact_matches_the_module() -> None:
    """Regenerate with the command in docs/v3-rollout-dashboard.md §8."""
    on_disk = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert on_disk == contract_dict()


# --- acceptance 1: the six required dimensions are covered --------------------


def test_every_required_dimension_has_a_panel() -> None:
    for dimension in REQUIRED_DIMENSIONS:
        assert dimension in V3_ROLLOUT_PANELS, dimension
    # The six named by V3-023 acceptance 1, spelled out so a rename cannot
    # silently drop one.
    assert {dimension.value for dimension in REQUIRED_DIMENSIONS} == {
        "decision_completion",
        "preview_usefulness",
        "repeat_use",
        "hidden_quality_rate",
        "latency",
        "cost_per_useful_decision",
    }


def test_every_panel_metric_exists_in_the_frozen_contract() -> None:
    for dimension, spec in V3_ROLLOUT_PANELS.items():
        for metric in spec.metrics:
            assert metric in V3_ANALYTICS_METRICS, f"{dimension}: unknown metric {metric}"


def test_every_frozen_metric_is_surfaced_by_exactly_one_panel() -> None:
    """No metric V3-002 froze may go unsurfaced, and none may be double-counted:
    a metric appearing on two panels would be read as two independent signals."""
    seen: dict[str, str] = {}
    for dimension, spec in V3_ROLLOUT_PANELS.items():
        for metric in spec.metrics:
            assert metric not in seen, f"{metric} on both {seen.get(metric)} and {dimension}"
            seen[metric] = dimension.value
    assert set(seen) == set(V3_ANALYTICS_METRICS), set(V3_ANALYTICS_METRICS) ^ set(seen)


# --- acceptance 2: computable from collected events ---------------------------


def test_every_metric_field_resolves_to_an_event_field_or_the_envelope() -> None:
    """A metric that names a field no event carries is not computable, however
    convincing its prose definition reads."""
    for name, spec in V3_ANALYTICS_METRICS.items():
        reachable = _fields_reachable_from(spec.events)
        for field_name in spec.fields:
            assert field_name in reachable, f"{name}: field '{field_name}' is on none of {spec.events}"


def test_every_segment_by_field_resolves_within_its_panel() -> None:
    """A segment the panel's own events cannot supply would render as one bucket."""
    for dimension, spec in V3_ROLLOUT_PANELS.items():
        events = tuple({event for metric in spec.metrics for event in V3_ANALYTICS_METRICS[metric].events})
        reachable = _fields_reachable_from(events)
        for field_name in spec.segment_by:
            assert field_name in reachable, f"{dimension}: cannot segment by '{field_name}'"


def test_no_field_the_dashboard_reads_is_a_forbidden_name() -> None:
    """The privacy contract holds through the dashboard too: aggregation is not
    an exemption from it."""
    for dimension, spec in V3_ROLLOUT_PANELS.items():
        for field_name in spec.segment_by:
            assert field_name not in FORBIDDEN_FIELD_NAMES, f"{dimension}.{field_name}"
    for name, spec in V3_ANALYTICS_METRICS.items():
        for field_name in spec.fields:
            assert field_name not in FORBIDDEN_FIELD_NAMES, f"{name}.{field_name}"


def test_every_kill_criterion_is_covered_exactly_once() -> None:
    coverage = kill_criteria_coverage()
    assert set(coverage) == {str(number) for number in range(1, KILL_CRITERIA_COUNT + 1)}
    for number, entry in coverage.items():
        if entry["coverage"] == "metric":
            assert entry["metrics"], f"KC{number}: claims metric coverage with no metric"
            assert entry["reason"] is None, f"KC{number}: metric-backed and also excused"
        else:
            assert entry["coverage"] == "qualitative", number
            assert entry["reason"], f"KC{number}: qualitative with no reason"
            assert not entry["metrics"], f"KC{number}: qualitative but names metrics"


def test_metric_backed_kill_criteria_reach_a_panel() -> None:
    """Computable is not enough — the number has to appear somewhere a human
    reads, or the criterion is unwatched in practice."""
    for number, entry in kill_criteria_coverage().items():
        if entry["coverage"] == "metric":
            assert entry["panels"], f"KC{number}: computable but on no panel"


def test_qualitative_kill_criteria_match_the_frozen_declaration() -> None:
    """V3-023 may not quietly re-classify a criterion V3-002 declared
    unmeasurable, in either direction."""
    coverage = kill_criteria_coverage()
    qualitative = {number for number, entry in coverage.items() if entry["coverage"] == "qualitative"}
    assert qualitative == {str(number) for number in QUALITATIVE_ONLY_KILL_CRITERIA}


# --- the emitter link: an event with no emitter collects nothing --------------


def _be_emitted_events() -> set[str]:
    """Event names passed to ``emit_v3_event`` anywhere under ``app/``."""
    pattern = re.compile(r"emit_v3_event\(\s*V3AnalyticsEventName\.([A-Z_]+)\.value")
    emitted: set[str] = set()
    for path in APP_ROOT.rglob("*.py"):
        for member in pattern.findall(path.read_text(encoding="utf-8")):
            emitted.add(V3AnalyticsEventName[member].value)
    return emitted


def test_every_be_owned_event_has_a_be_emitter() -> None:
    expected = {name.value for name, spec in V3_ANALYTICS_EVENTS.items() if spec.owner == "be"}
    assert _be_emitted_events() >= expected, expected - _be_emitted_events()


def test_the_backend_does_not_emit_fe_owned_events() -> None:
    """``EventSpec`` promises exactly one emitting side per event. Two sides
    emitting one event would double every rate computed from it."""
    fe_owned = {name.value for name, spec in V3_ANALYTICS_EVENTS.items() if spec.owner == "fe"}
    assert not (_be_emitted_events() & fe_owned), _be_emitted_events() & fe_owned


# --- acceptance 4: verified capability vs pending approvals -------------------


def test_readiness_gate_invariants_hold() -> None:
    for gate in READINESS_GATES:
        if gate.status is ReadinessStatus.VERIFIED:
            assert gate.evidence, f"verified with no evidence: {gate.statement}"
            assert gate.blocker is None, f"verified but blocked: {gate.statement}"
        else:
            assert gate.blocker, f"{gate.status.value} with no blocker: {gate.statement}"


def test_every_spec_readiness_group_is_represented() -> None:
    covered = {gate.group for gate in READINESS_GATES}
    assert covered == set(ReadinessGroup), set(ReadinessGroup) - covered


def test_no_gate_is_verified_on_visual_or_product_value() -> None:
    """The two groups that need a real provider and real users. If a future edit
    marks one verified, this test forces it to be a deliberate act."""
    for gate in READINESS_GATES:
        if gate.group in {ReadinessGroup.VISUAL, ReadinessGroup.PRODUCT_VALUE}:
            assert gate.status is not ReadinessStatus.VERIFIED, gate.statement


def test_every_human_gate_reference_resolves() -> None:
    for gate in READINESS_GATES:
        if gate.human_gate is not None:
            assert gate.human_gate in OPEN_HUMAN_GATES, gate.human_gate
    for prerequisite in PIPELINE_PREREQUISITES:
        if prerequisite.human_gate is not None:
            assert prerequisite.human_gate in OPEN_HUMAN_GATES, prerequisite.human_gate
    assert COST_MODEL.human_gate in OPEN_HUMAN_GATES


def test_every_open_human_gate_blocks_a_real_dimension() -> None:
    known = {dimension.value for dimension in RolloutDimension}
    for name, spec in OPEN_HUMAN_GATES.items():
        assert spec.decision and spec.why_human, name
        for blocked in spec.blocks:
            assert blocked in known, f"{name} blocks unknown dimension {blocked}"


# --- the §18 Phase 1 seed study, which is what would discharge those gates ----


def test_the_seed_study_covers_all_five_spec_questions() -> None:
    """V3_SPEC §18 Phase 1 lists five. Fewer means the report's "seed-user" half
    silently narrowed the spec's own study."""
    assert len(SEED_STUDY_QUESTIONS) == 5
    for question in SEED_STUDY_QUESTIONS:
        assert question.question.strip()
        assert question.what_to_observe.strip(), question.question


def test_every_seed_question_discharges_or_corroborates_something() -> None:
    """A question that moves no gate and feeds no metric is not worth a seed
    round's attention, however interesting it reads."""
    for question in SEED_STUDY_QUESTIONS:
        assert question.discharges or question.supporting_metric, question.question


def test_seed_discharge_targets_are_real_and_not_already_verified() -> None:
    """Discharging an already-verified gate would inflate what the study buys."""
    by_statement = {gate.statement: gate for gate in READINESS_GATES}
    for question in SEED_STUDY_QUESTIONS:
        for statement in question.discharges:
            gate = by_statement.get(statement)
            assert gate is not None, f"{question.question}: no such gate {statement!r}"
            assert gate.status is not ReadinessStatus.VERIFIED, statement


def test_every_seed_blocked_gate_is_covered_by_a_seed_question() -> None:
    """The invariant that makes this a plan rather than a wish list: if a gate is
    waiting on the seed_cohort human gate, the study must actually answer it."""
    dischargeable = {statement for question in SEED_STUDY_QUESTIONS for statement in question.discharges}
    waiting = {gate.statement for gate in READINESS_GATES if gate.human_gate == "seed_cohort"}
    assert waiting <= dischargeable, waiting - dischargeable


def test_seed_supporting_metrics_are_frozen_metrics() -> None:
    for question in SEED_STUDY_QUESTIONS:
        if question.supporting_metric is not None:
            assert question.supporting_metric in V3_ANALYTICS_METRICS, question.supporting_metric


# --- acceptance 3: nothing here turns anything on -----------------------------


def test_cost_per_useful_decision_carries_no_invented_price() -> None:
    """A placeholder number here would read as an approved budget."""
    assert COST_MODEL.unit_price_per_generation is None
    assert COST_MODEL.currency is None
    assert COST_MODEL.generation_metric in V3_ANALYTICS_METRICS


def test_no_v3_flag_is_enabled_in_committed_configuration() -> None:
    """Acceptance 3, as a check rather than a sentence in a report."""
    from app.config.settings import Settings

    for flag in ("ENABLE_V3_PURCHASE_DECISION", "ENABLE_V3_AVATAR", "ENABLE_V3_STATIC_TRYON"):
        assert Settings.model_fields[flag].default is False, flag


def test_the_sink_gap_is_declared_unmet() -> None:
    """The dashboard must never imply it has data. Two prerequisites are unmet by
    fact (no sink) and by design (flags off); both must stay declared."""
    unmet = {spec.requirement: spec for spec in PIPELINE_PREREQUISITES if not spec.met}
    assert unmet, "every prerequisite met would claim the dashboard is live"
    assert any(spec.human_gate == "telemetry_sink" for spec in unmet.values())
    assert any(spec.human_gate == "flag_enablement" for spec in unmet.values())
