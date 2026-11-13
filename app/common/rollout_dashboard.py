"""V3 rollout dashboard contract (V3-023).

The rollout gate for V3 is usefulness, trust, fallback and cost — never
generation volume (``V3_SPEC`` §18–§20). This module is the machine-readable
form of that gate: every panel is defined purely in terms of the metrics frozen
by V3-002, every §20 kill criterion maps to either a metric or an explicit
"no honest metric exists" reason, every §19 release threshold carries a status
that separates capability this program has actually verified from approvals
still owed to a person, and every §18 Phase 1 seed question names the thresholds
answering it would discharge.

It deliberately ships no query engine, no UI and no sink. What it ships is the
contract an analyst (or a later warehouse job) implements, plus the tests that
keep it computable end to end:

    kill criterion -> metric -> event field -> event -> emitter on the owning
    side -> sink

Every link in that chain is asserted by ``tests/unit/test_v3_rollout_dashboard.py``
except the last one, which is :data:`OPEN_HUMAN_GATES`' ``telemetry_sink``.
That is the honest state of this dashboard: fully specified, not yet fed.

The artifact ``docs/v3-rollout-dashboard.json`` is generated from this module and
shared byte-identically with aidrobe-FE (``docs/v3-rollout-dashboard.md`` §8 has
the regeneration command). This module is the generator of record.

NOTHING HERE ENABLES A ROLLOUT. Every V3 feature flag stays off in committed
configuration; :data:`OPEN_HUMAN_GATES` lists what a person must answer first.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from app.common.analytics_events import CONTRACT_NAME as EVENTS_CONTRACT_NAME
from app.common.analytics_events import CONTRACT_SCHEMA_VERSION as EVENTS_CONTRACT_SCHEMA_VERSION
from app.common.analytics_events import (
    KILL_CRITERIA_COUNT,
    QUALITATIVE_ONLY_KILL_CRITERIA,
    V3_ANALYTICS_METRICS,
)

CONTRACT_NAME = "aidrobe-v3-rollout-dashboard"
CONTRACT_SCHEMA_VERSION = 1
CONTRACT_PATH = Path(__file__).resolve().parents[2] / "docs" / "v3-rollout-dashboard.json"


class RolloutDimension(StrEnum):
    """Dashboard panels.

    The first six are required by V3-023 acceptance 1 and are listed in
    :data:`REQUIRED_DIMENSIONS`. The last two exist because V3-002 froze
    kill-criteria metrics for visual trust (§20.1/§20.2) and consent cost
    (§20.4) that none of the six required dimensions names — dropping them would
    leave a frozen metric unsurfaced and two kill criteria unwatched.
    """

    DECISION_COMPLETION = "decision_completion"
    PREVIEW_USEFULNESS = "preview_usefulness"
    REPEAT_USE = "repeat_use"
    HIDDEN_QUALITY_RATE = "hidden_quality_rate"
    LATENCY = "latency"
    COST_PER_USEFUL_DECISION = "cost_per_useful_decision"
    VISUAL_TRUST = "visual_trust"
    CONSENT_COST = "consent_cost"


REQUIRED_DIMENSIONS: tuple[RolloutDimension, ...] = (
    RolloutDimension.DECISION_COMPLETION,
    RolloutDimension.PREVIEW_USEFULNESS,
    RolloutDimension.REPEAT_USE,
    RolloutDimension.HIDDEN_QUALITY_RATE,
    RolloutDimension.LATENCY,
    RolloutDimension.COST_PER_USEFUL_DECISION,
)


class ReadinessStatus(StrEnum):
    """How much of a §19 threshold this program has actually established.

    ``MECHANISM_ONLY`` is the category that keeps this report honest. A test can
    prove a failed preview never erases the decision (the code path); it cannot
    prove that failures do not depress the completion *rate* in the field. Those
    are different claims and V3-022 only earned the first.
    """

    VERIFIED = "verified"
    MECHANISM_ONLY = "mechanism_only"
    UNVERIFIED = "unverified"


class ReadinessGroup(StrEnum):
    """``V3_SPEC`` §19's own four groups, kept as-is so the report is auditable
    against the spec rather than against a restructuring of it."""

    VISUAL = "visual"
    EXPERIENCE = "experience"
    PRODUCT_VALUE = "product_value"
    COST = "cost"


@dataclass(frozen=True)
class PanelSpec:
    """One dashboard panel: a product question and the metrics that answer it."""

    question: str
    headline_metric: str
    supporting_metrics: tuple[str, ...] = ()
    segment_by: tuple[str, ...] = ()
    spec_ref: str = ""

    @property
    def metrics(self) -> tuple[str, ...]:
        return (self.headline_metric, *self.supporting_metrics)

    def to_contract(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "headline_metric": self.headline_metric,
            "supporting_metrics": list(self.supporting_metrics),
            "segment_by": list(self.segment_by),
            "spec_ref": self.spec_ref,
        }


@dataclass(frozen=True)
class ReadinessGate:
    """One ``V3_SPEC`` §19 release threshold with its evidence or its blocker.

    Invariants (asserted in tests): ``VERIFIED`` requires evidence and forbids a
    blocker; anything else requires a blocker. So a threshold can never be
    silently neither-verified-nor-explained.
    """

    group: ReadinessGroup
    statement: str
    status: ReadinessStatus
    evidence: tuple[str, ...] = ()
    blocker: str | None = None
    human_gate: str | None = None

    def to_contract(self) -> dict[str, Any]:
        return {
            "group": self.group.value,
            "statement": self.statement,
            "status": self.status.value,
            "evidence": list(self.evidence),
            "blocker": self.blocker,
            "human_gate": self.human_gate,
        }


@dataclass(frozen=True)
class HumanGateSpec:
    """A decision this loop must not make for itself (workspace CLAUDE.md)."""

    decision: str
    why_human: str
    blocks: tuple[str, ...] = field(default_factory=tuple)

    def to_contract(self) -> dict[str, Any]:
        return {"decision": self.decision, "why_human": self.why_human, "blocks": list(self.blocks)}


@dataclass(frozen=True)
class PrerequisiteSpec:
    """Something that must hold before any panel can show a number."""

    requirement: str
    met: bool
    detail: str
    human_gate: str | None = None

    def to_contract(self) -> dict[str, Any]:
        return {
            "requirement": self.requirement,
            "met": self.met,
            "detail": self.detail,
            "human_gate": self.human_gate,
        }


# --- panels ------------------------------------------------------------------
# Every metric named here is a key of V3_ANALYTICS_METRICS (V3-002) and every
# name in `segment_by` resolves to the envelope or to a field carried by at
# least one of the panel's events. Both are asserted, not assumed.

V3_ROLLOUT_PANELS: Mapping[RolloutDimension, PanelSpec] = {
    RolloutDimension.DECISION_COMPLETION: PanelSpec(
        question="Do users who start a purchase judgment finish one, and does it change what they do?",
        headline_metric="decision_completion_rate",
        supporting_metrics=("decision_action_rate", "existing_item_alternative_rate"),
        segment_by=("platform", "source_type", "visual_need"),
        spec_ref="V3_SPEC §18 Phase 2; §19 experience",
    ),
    RolloutDimension.PREVIEW_USEFULNESS: PanelSpec(
        question=(
            "After seeing a preview, does the user act on the decision — and when the preview "
            "cannot be shown, does the decision still carry them?"
        ),
        headline_metric="post_preview_decision_action_rate",
        supporting_metrics=("preview_request_rate", "fallback_decision_retention_rate"),
        segment_by=("platform", "concern", "avatar_status"),
        spec_ref="V3_SPEC §19 product value; §20.3; §20.5",
    ),
    RolloutDimension.REPEAT_USE: PanelSpec(
        question="Do users come back to the decision flow on another day?",
        headline_metric="repeat_use_rate",
        segment_by=("platform", "source_type"),
        spec_ref="V3_SPEC §18 Phase 2; §19 product value",
    ),
    RolloutDimension.HIDDEN_QUALITY_RATE: PanelSpec(
        question=(
            "How often is a generated preview withheld from the user, and is that the quality "
            "gate working or the provider failing?"
        ),
        headline_metric="preview_hidden_rate",
        segment_by=("platform", "generation_status"),
        spec_ref="V3_SPEC §19 visual; §20.2; §20.9",
    ),
    RolloutDimension.LATENCY: PanelSpec(
        question="How long does a decision take, and does asking for a preview slow it down?",
        headline_metric="time_to_decision_seconds",
        segment_by=("platform", "visual_need"),
        spec_ref="V3_SPEC §18 Phase 2; §20.5",
    ),
    RolloutDimension.COST_PER_USEFUL_DECISION: PanelSpec(
        question="What does one decision that actually changed behavior cost in paid generations?",
        headline_metric="generations_per_useful_decision",
        supporting_metrics=("preview_cache_hit_rate",),
        segment_by=("platform", "cache_hit"),
        spec_ref="V3_SPEC §19 cost; §20.7",
    ),
    RolloutDimension.VISUAL_TRUST: PanelSpec(
        question="Do users recognize themselves, and do they say the garment came out wrong?",
        headline_metric="identity_fidelity_rate",
        supporting_metrics=("garment_fidelity_complaint_rate",),
        segment_by=("platform", "verdict"),
        spec_ref="V3_SPEC §19 visual; §20.1; §20.2",
    ),
    RolloutDimension.CONSENT_COST: PanelSpec(
        question="Does asking for a photo cost us decisions, and who agrees to it?",
        headline_metric="consent_prompt_completion_delta",
        supporting_metrics=("consent_acceptance_rate",),
        segment_by=("platform", "context", "source_type"),
        spec_ref="V3_SPEC §19 experience; §20.4",
    ),
}


# --- cost model --------------------------------------------------------------
# "Cost per useful decision" is the only required dimension that cannot be
# expressed in events alone: events can count paid generations, but turning a
# count into money needs a provider price, and choosing a provider is a human
# gate. So the dashboard carries the formula with the price slot explicitly
# EMPTY rather than a placeholder number that would read like a budget.


@dataclass(frozen=True)
class CostModelSpec:
    formula: str
    generation_metric: str
    unit_price_per_generation: float | None
    currency: str | None
    human_gate: str

    def to_contract(self) -> dict[str, Any]:
        return {
            "formula": self.formula,
            "generation_metric": self.generation_metric,
            "unit_price_per_generation": self.unit_price_per_generation,
            "currency": self.currency,
            "human_gate": self.human_gate,
        }


COST_MODEL = CostModelSpec(
    formula="cost_per_useful_decision = generations_per_useful_decision * unit_price_per_generation",
    generation_metric="generations_per_useful_decision",
    unit_price_per_generation=None,
    currency=None,
    human_gate="provider_choice",
)


# --- open human gates --------------------------------------------------------

OPEN_HUMAN_GATES: Mapping[str, HumanGateSpec] = {
    "provider_choice": HumanGateSpec(
        decision="Which production static try-on provider to use, and at what per-generation price.",
        why_human="Spend and a third-party data processor. Development has only ever run the fake adapter.",
        blocks=("cost_per_useful_decision", "visual_trust", "hidden_quality_rate"),
    ),
    "spending_budget": HumanGateSpec(
        decision="The per-user and per-day generation budget, and the ceiling that stops a rollout.",
        why_human="A budget is a business commitment; the cost guard can enforce one but must not invent it.",
        blocks=("cost_per_useful_decision",),
    ),
    "telemetry_sink": HumanGateSpec(
        decision="Which analytics sink receives V3 events, with what retention.",
        why_human=(
            "A third-party processor for behavioral data, plus a retention period with privacy "
            "consequences. Until it is chosen, every panel on this dashboard is empty."
        ),
        blocks=tuple(dimension.value for dimension in RolloutDimension),
    ),
    "privacy_copy": HumanGateSpec(
        decision="The final photo-consent and privacy wording shown before a photo is used.",
        why_human="Legally operative user-facing text.",
        blocks=("consent_cost",),
    ),
    "seed_cohort": HumanGateSpec(
        decision="Whether to recruit Phase 1 seed users and process their real photos.",
        why_human=(
            "Development loops are forbidden from sending real user photos to a provider "
            "(workspace CLAUDE.md), so the first real-photo run is a human decision."
        ),
        blocks=("visual_trust", "preview_usefulness", "repeat_use"),
    ),
    "flag_enablement": HumanGateSpec(
        decision="Turning ENABLE_V3_PURCHASE_DECISION / ENABLE_V3_AVATAR / ENABLE_V3_STATIC_TRYON on in production.",
        why_human="The rollout itself. All three stay off in committed configuration.",
        blocks=tuple(dimension.value for dimension in RolloutDimension),
    ),
    "fit_accuracy_claim": HumanGateSpec(
        decision="Whether any size or fit accuracy claim may ever be made.",
        why_human=(
            "V3_SPEC §21 forbids a pseudo-precise fit score and §20.6 treats a size "
            "misunderstanding as a kill criterion. The default answer is no."
        ),
        blocks=("visual_trust",),
    ),
    "retention_reduction": HumanGateSpec(
        decision="Any destructive migration or reduction of the photo/render retention policy.",
        why_human="Irreversible and privacy-relevant.",
        blocks=(),
    ),
}


# --- pipeline prerequisites --------------------------------------------------
# Why every panel is empty today, stated as data so the readiness report cannot
# quietly imply that numbers exist.

PIPELINE_PREREQUISITES: tuple[PrerequisiteSpec, ...] = (
    PrerequisiteSpec(
        requirement="A frozen event contract shared by both repos.",
        met=True,
        detail="V3-002: 11 events, a 5-field envelope and 15 metrics, byte-identical in aidrobe-BE and aidrobe-FE.",
    ),
    PrerequisiteSpec(
        requirement="An emitter for every event, on the side the contract declares as owner.",
        met=True,
        detail=(
            "All 11 events are emitted: the 4 be-owned ones through app/common/analytics_emit.py, "
            "the 7 fe-owned ones through trackV3. Asserted by the emitter-coverage tests on both sides."
        ),
    ),
    PrerequisiteSpec(
        requirement="A sink that persists emitted events somewhere queryable.",
        met=False,
        detail=(
            "FE #157 registers a default-off, allowlisted release dispatcher and an authenticated BE relay; "
            "BE API and worker emitters share a bounded HTTP delivery port. Synthetic local query evidence "
            "proves wiring, not a deployed durable collector. Collector selection, per-environment "
            "credentials, retention/access/deletion approval and real paired-build delivery verification "
            "remain open. No production event collection is enabled and this prerequisite remains unmet."
        ),
        human_gate="telemetry_sink",
    ),
    PrerequisiteSpec(
        requirement="V3 flags enabled for a cohort, so events are produced at all.",
        met=False,
        detail=(
            "All three ENABLE_V3_* flags are False in committed configuration on both sides. "
            "This prerequisite is deliberately unmet — V3-023 acceptance 3 requires it."
        ),
        human_gate="flag_enablement",
    ),
)


# --- §19 readiness gates -----------------------------------------------------
# Statuses are graded against evidence that exists in this repository or in the
# program's recorded artifacts. `MECHANISM_ONLY` means the code path is proven
# and the field outcome is not.

READINESS_GATES: tuple[ReadinessGate, ...] = (
    # 视觉 / visual — §19
    ReadinessGate(
        group=ReadinessGroup.VISUAL,
        statement="The internal quality gate passes at a rate that can be operated stably.",
        status=ReadinessStatus.MECHANISM_ONLY,
        evidence=(
            "V3-018 implemented the gate and the no-result fallback",
            "tests/api/test_v3_e2e_journeys.py::test_a_quality_rejection_never_erases_the_decision",
        ),
        blocker=(
            "The pass RATE is a property of a real provider's output. Every run so far used the "
            "fake adapter, which composites a stub instead of generating, so no rate observed here "
            "predicts a production one."
        ),
        human_gate="provider_choice",
    ),
    ReadinessGate(
        group=ReadinessGroup.VISUAL,
        statement="Most seed users say the render basically looks like them.",
        status=ReadinessStatus.UNVERIFIED,
        blocker="Needs real photos, a real provider and seed users. identity_fidelity_rate is defined and has no data.",
        human_gate="seed_cohort",
    ),
    ReadinessGate(
        group=ReadinessGroup.VISUAL,
        statement="Most seed users find garment color and overall silhouette believable.",
        status=ReadinessStatus.UNVERIFIED,
        blocker="Same as above; garment_fidelity_complaint_rate is defined and has no data.",
        human_gate="seed_cohort",
    ),
    ReadinessGate(
        group=ReadinessGroup.VISUAL,
        statement="Obvious body beautification, age drift or garment deformation is very rare.",
        status=ReadinessStatus.UNVERIFIED,
        blocker=(
            "No metric can carry this — the contract forbids body inferences as event fields by "
            "design, so it is a benchmark-review judgment on real provider output."
        ),
        human_gate="provider_choice",
    ),
    # 体验 / experience — §19
    ReadinessGate(
        group=ReadinessGroup.EXPERIENCE,
        statement="An avatar-ready user reaches a preview with at most one extra explicit action.",
        status=ReadinessStatus.VERIFIED,
        evidence=(
            "tests/api/test_v3_e2e_journeys.py::test_an_avatar_ready_user_reaches_a_cached_preview_with_one_action",
            "V3-022 FE driver shot 06 — one tap, render on screen, decision still under it",
        ),
    ),
    ReadinessGate(
        group=ReadinessGroup.EXPERIENCE,
        statement="A user with no avatar still completes the purchase judgment in full.",
        status=ReadinessStatus.VERIFIED,
        evidence=(
            "tests/api/test_v3_e2e_journeys.py::test_a_user_with_no_avatar_completes_a_useful_decision",
            "V3-022 FE driver shots 01-02 — zero assets, zero try-on jobs, setup offered not required",
        ),
    ),
    ReadinessGate(
        group=ReadinessGroup.EXPERIENCE,
        statement="A failed preview does not reduce decision-flow completion.",
        status=ReadinessStatus.MECHANISM_ONLY,
        evidence=(
            "tests/api/test_v3_e2e_journeys.py::test_a_provider_failure_never_erases_the_decision",
            "V3-022 FE driver shots 03/04/05 — byte-identical frames across failure, rejection and unknown status",
        ),
        blocker=(
            "Proven: the decision SURVIVES a failure. Not proven: that failures do not depress the "
            "completion rate in the field. The declared metric for that is "
            "fallback_decision_retention_rate on the preview_usefulness panel, which joins "
            "v3_preview_viewed[result=fallback] to v3_decision_action on flow_id and needs data. "
            "Stated precisely, because it bounds what this dashboard can answer: the "
            "decision_completion panel CANNOT be segmented by preview outcome — its own events "
            "(v3_decision_started/completed/action) carry no generation_status, and declaring "
            "such a segment would fail test_every_segment_by_field_resolves_within_its_panel. A "
            "true completion-rate-with-failure vs without comparison therefore needs a "
            "cross-panel flow join that no single panel declares; the retention rate is the "
            "honest single-panel answer."
        ),
        human_gate="telemetry_sink",
    ),
    ReadinessGate(
        group=ReadinessGroup.EXPERIENCE,
        statement="The user clearly understands this is not a size guarantee.",
        status=ReadinessStatus.MECHANISM_ONLY,
        evidence=(
            "V3-020 renders the §22 boundary sentence on every preview",
            "V3_SPEC §21 forbids a fit score, and none is implemented",
        ),
        blocker=(
            "Shipping the sentence is not evidence of comprehension. §20.6 is qualitative-only by "
            "V3-002's own declaration: only moderated sessions or support reports can detect this."
        ),
        human_gate="seed_cohort",
    ),
    # 产品价值 / product value — §19
    ReadinessGate(
        group=ReadinessGroup.PRODUCT_VALUE,
        statement="Most users say the preview increased their purchase or wearing confidence.",
        status=ReadinessStatus.UNVERIFIED,
        blocker="A stated-preference question no in-app event answers. Needs the Phase 1 qualitative study.",
        human_gate="seed_cohort",
    ),
    ReadinessGate(
        group=ReadinessGroup.PRODUCT_VALUE,
        statement="It confirms, changes or vetoes a decision rather than being merely entertaining.",
        status=ReadinessStatus.UNVERIFIED,
        blocker=(
            "post_preview_decision_action_rate is defined precisely for this and has no data. "
            "This is the metric §20.3 would be killed on."
        ),
        human_gate="telemetry_sink",
    ),
    ReadinessGate(
        group=ReadinessGroup.PRODUCT_VALUE,
        statement="Users find it more helpful than single-garment try-on on shopping platforms.",
        status=ReadinessStatus.UNVERIFIED,
        blocker=(
            "§20.8 is qualitative-only by V3-002's declaration: a relative judgment with no in-app "
            "referent. Needs benchmark review against products the user has already seen."
        ),
        human_gate="seed_cohort",
    ),
    ReadinessGate(
        group=ReadinessGroup.PRODUCT_VALUE,
        statement="There is observable intent to use it again.",
        status=ReadinessStatus.UNVERIFIED,
        blocker="repeat_use_rate is defined and has no data.",
        human_gate="telemetry_sink",
    ),
    # 成本 / cost — §19
    ReadinessGate(
        group=ReadinessGroup.COST,
        statement="The cache hits effectively for a repeated garment/avatar combination.",
        status=ReadinessStatus.VERIFIED,
        evidence=(
            "tests/api/test_v3_e2e_journeys.py::test_an_avatar_ready_user_reaches_a_cached_preview_with_one_action"
            " — a second create POST returns the same asset_id and adds no job row",
            "V3-017 idempotent async jobs keyed on a stable combination key",
        ),
    ),
    ReadinessGate(
        group=ReadinessGroup.COST,
        statement="The static-image pass rate and the cost per useful decision are inside budget.",
        status=ReadinessStatus.UNVERIFIED,
        blocker=(
            "Two missing inputs, not one: there is no provider price (so COST_MODEL's unit price is "
            "null) and no budget to compare against."
        ),
        human_gate="spending_budget",
    ),
    ReadinessGate(
        group=ReadinessGroup.COST,
        statement="Video or multi-view is not a P0 prerequisite.",
        status=ReadinessStatus.VERIFIED,
        evidence=(
            "V3_SPEC §21 defers 360° video and multi-preview menus",
            "No feature in feature_list.json implements either; the try-on port is static-only (V3-016)",
        ),
    ),
)


# --- §18 Phase 1 seed study ---------------------------------------------------
# The dashboard says which gates are unverified; this says how the seed round
# that would verify them is supposed to earn it. Phase 1 is qualitative by
# design — "目标不是证明增长，而是验证可用性" — so a question's product is a
# discharged §19 gate, not a number, and `supporting_metric` is corroboration
# once a sink exists rather than the answer itself.


@dataclass(frozen=True)
class SeedStudyQuestion:
    """One ``V3_SPEC`` §18 Phase 1 question and what answering it would buy.

    Invariants (asserted): every name in ``discharges`` is the statement of a
    real :data:`READINESS_GATES` entry that is NOT already verified, every
    ``supporting_metric`` is a metric V3-002 froze, and every gate whose blocker
    is the ``seed_cohort`` human gate is discharged by at least one question.
    That last one is what makes this a plan rather than a wish list: it fails if
    the study as designed would leave a seed-blocked gate still unanswered.
    """

    question: str
    what_to_observe: str
    discharges: tuple[str, ...] = ()
    supporting_metric: str | None = None
    spec_ref: str = "V3_SPEC §18 Phase 1"

    def to_contract(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "what_to_observe": self.what_to_observe,
            "discharges": list(self.discharges),
            "supporting_metric": self.supporting_metric,
            "spec_ref": self.spec_ref,
        }


SEED_STUDY_QUESTIONS: tuple[SeedStudyQuestion, ...] = (
    SeedStudyQuestion(
        question="Do users understand how this differs from single-garment try-on on a shopping platform?",
        what_to_observe=(
            "Unprompted, whether the user describes it as a whole-outfit judgment against their own "
            "wardrobe rather than as a garment fitting — and whether they read the §22 boundary "
            "sentence as a caveat or as a size promise."
        ),
        discharges=(
            "Users find it more helpful than single-garment try-on on shopping platforms.",
            "The user clearly understands this is not a size guarantee.",
        ),
    ),
    SeedStudyQuestion(
        question="Are users willing to authorize a photo, and where do they stop?",
        what_to_observe=(
            "The point at which a user declines: the ask itself, the consent copy, or the photo "
            "capture. Declining is a valid outcome — the no-avatar path must stay complete."
        ),
        supporting_metric="consent_acceptance_rate",
    ),
    SeedStudyQuestion(
        question="At which moment do users most want the preview?",
        what_to_observe=(
            "Where in the flow the user asks for it unprompted, and whether they ever want it "
            "BEFORE reading the text decision — which would contradict the on-demand design."
        ),
        supporting_metric="preview_request_rate",
    ),
    SeedStudyQuestion(
        question="Does the preview change or confirm a purchase decision, and does the user say it helped?",
        what_to_observe=(
            "Two things, recorded separately, because they discharge two different thresholds. "
            "(a) A decision stated before the preview and restated after it, so a reversal or a "
            "hardened confirmation is OBSERVED rather than self-reported; entertainment with no "
            "decision movement is kill criterion 3. (b) A direct question afterwards — did seeing "
            "it make you more or less confident about buying this, or about wearing it — because "
            "the confidence threshold is a STATED preference and observed behavior cannot stand "
            "in for it. Recording only (a) would let a completed study promote the confidence "
            "gate without ever collecting what its statement asks for."
        ),
        discharges=(
            "Most users say the preview increased their purchase or wearing confidence.",
            "It confirms, changes or vetoes a decision rather than being merely entertaining.",
        ),
        supporting_metric="post_preview_decision_action_rate",
    ),
    SeedStudyQuestion(
        question="Which distortions destroy trust immediately?",
        what_to_observe=(
            "The first thing the user names as wrong, and whether they keep using the render after "
            "saying it. Ranked failure modes are what a provider choice is actually made on."
        ),
        discharges=(
            "Most seed users say the render basically looks like them.",
            "Most seed users find garment color and overall silhouette believable.",
        ),
        supporting_metric="garment_fidelity_complaint_rate",
    ),
)


def contract_dict() -> dict[str, Any]:
    """Exact structure of ``docs/v3-rollout-dashboard.json`` (drift guard)."""
    return {
        "contract": CONTRACT_NAME,
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "source_contract": EVENTS_CONTRACT_NAME,
        "source_schema_version": EVENTS_CONTRACT_SCHEMA_VERSION,
        "required_dimensions": [dimension.value for dimension in REQUIRED_DIMENSIONS],
        "panels": {dimension.value: spec.to_contract() for dimension, spec in V3_ROLLOUT_PANELS.items()},
        "cost_model": COST_MODEL.to_contract(),
        "kill_criteria": kill_criteria_coverage(),
        "pipeline_prerequisites": [spec.to_contract() for spec in PIPELINE_PREREQUISITES],
        "readiness_gates": [gate.to_contract() for gate in READINESS_GATES],
        "seed_study": [question.to_contract() for question in SEED_STUDY_QUESTIONS],
        "open_human_gates": {name: spec.to_contract() for name, spec in sorted(OPEN_HUMAN_GATES.items())},
    }


def kill_criteria_coverage() -> dict[str, dict[str, Any]]:
    """Map every ``V3_SPEC`` §20 criterion to what watches it.

    A criterion is either ``metric`` — with the metrics that compute it and the
    panels that surface them — or ``qualitative``, carrying V3-002's reason why
    no event can honestly measure it. Never both, never neither; the tests
    assert that partition over 1..:data:`KILL_CRITERIA_COUNT`.
    """
    panel_of_metric: dict[str, list[str]] = {}
    for dimension, spec in V3_ROLLOUT_PANELS.items():
        for metric in spec.metrics:
            panel_of_metric.setdefault(metric, []).append(dimension.value)

    coverage: dict[str, dict[str, Any]] = {}
    for number in range(1, KILL_CRITERIA_COUNT + 1):
        reason = QUALITATIVE_ONLY_KILL_CRITERIA.get(number)
        if reason is not None:
            coverage[str(number)] = {"coverage": "qualitative", "reason": reason, "metrics": [], "panels": []}
            continue
        metrics = sorted(name for name, spec in V3_ANALYTICS_METRICS.items() if number in spec.kill_criteria)
        panels = sorted({panel for name in metrics for panel in panel_of_metric.get(name, [])})
        coverage[str(number)] = {"coverage": "metric", "reason": None, "metrics": metrics, "panels": panels}
    return coverage
