"""V3 analytics event contract (V3-002).

Python mirror of the cross-repo event contract shared byte-identically with
aidrobe-FE via ``docs/v3-analytics-events.json`` (see
``docs/v3-analytics-events.md`` for semantics, privacy rules and the
kill-criteria metrics). The FE mirror is ``src/utils/v3AnalyticsEvents.ts``.
This module is the generator of record for the JSON artifact;
``tests/unit/test_v3_analytics_events.py`` guards against drift.

Privacy invariant: every event field is a closed value space (enum, boolean,
bounded integer, uuid or timestamp). Free text does not exist in this
contract, so photo URLs, prompt content, body inferences or any other
sensitive value cannot be carried by a valid event.

This module defines the contract only. Emitters arrive with the features that
own the surfaces (V3-008 decision, V3-013 avatar, V3-019 preview) and must
call :func:`validate_v3_event` before dispatch.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

CONTRACT_NAME = "aidrobe-v3-analytics-events"
CONTRACT_SCHEMA_VERSION = 1
CONTRACT_PATH = Path(__file__).resolve().parents[2] / "docs" / "v3-analytics-events.json"

# Flow correlation travels on the existing correlation header (CorrelationIdMiddleware)
# so analytics never changes an API shape frozen by V3-001 (v3-p0-contract §8).
FLOW_ID_HEADER = "X-Correlation-Id"
_FLOW_ID_NAMESPACE = uuid.uuid5(uuid.NAMESPACE_URL, "https://aidrobe.app/v3/analytics/flow-id")


class V3AnalyticsEventName(StrEnum):
    """Frozen V3 event names. FE uses the same literals (src/utils/v3AnalyticsEvents.ts)."""

    DECISION_STARTED = "v3_decision_started"
    DECISION_COMPLETED = "v3_decision_completed"
    DECISION_VIEWED = "v3_decision_viewed"
    DECISION_ACTION = "v3_decision_action"
    AVATAR_PROMPT_SHOWN = "v3_avatar_prompt_shown"
    AVATAR_CONSENT = "v3_avatar_consent"
    AVATAR_DELETED = "v3_avatar_deleted"
    PREVIEW_REQUESTED = "v3_preview_requested"
    PREVIEW_OUTCOME = "v3_preview_outcome"
    PREVIEW_VIEWED = "v3_preview_viewed"
    PREVIEW_FEEDBACK = "v3_preview_feedback"


class V3EventGroup(StrEnum):
    """Decision value vs visual-generation usage must stay separable (V3-002
    acceptance 1): every ``decision`` metric is computable with zero ``preview``
    events, so a program with the visual flags off still reports its value."""

    DECISION = "decision"
    AVATAR = "avatar"
    PREVIEW = "preview"


class V3FlowIdRule(StrEnum):
    """How an event relates to a decision/preview flow.

    ``when_in_flow`` exists because the avatar prompt appears in two places:
    inside a preview request (part of a decision flow, so the flow_id must be
    attached or kill-criterion 4 becomes uncomputable) and after an unrelated
    OOTD/import flow (no decision flow exists at all).
    """

    REQUIRED = "required"
    WHEN_IN_FLOW = "when_in_flow"
    NOT_APPLICABLE = "not_applicable"


class V3Platform(StrEnum):
    IOS = "ios"
    ANDROID = "android"
    BACKEND = "backend"


class V3DecisionSource(StrEnum):
    """Candidate source (v3-p0-contract §5.2)."""

    SHARE_LINK = "share_link"
    SCREENSHOT = "screenshot"
    ORDER_IMAGE = "order_image"
    ASK_LINK = "ask_link"


class V3Decision(StrEnum):
    """Decision states (v3-p0-contract §5.3, verbatim from V3-007 acceptance)."""

    WORTH_ADDING = "worth_adding"
    CONSIDER_ALTERNATE = "consider_alternate"
    WAIT = "wait"
    AVOID_DUPLICATE = "avoid_duplicate"
    INSUFFICIENT_INFORMATION = "insufficient_information"


class V3Uncertainty(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class V3VisualNeed(StrEnum):
    NONE = "none"
    HELPFUL = "helpful"
    RECOMMENDED = "recommended"


class V3ParseStatus(StrEnum):
    """Terminal candidate parse states only; `submitted` never reaches a decision event."""

    PARSED = "parsed"
    PARSE_FAILED = "parse_failed"


class V3DecisionAction(StrEnum):
    """User next-actions (v3-p0-contract §5.3 `next_actions`, verbatim)."""

    ADD_TO_CANDIDATES = "add_to_candidates"
    SKIP = "skip"
    RECHECK_COLOR = "recheck_color"
    STYLE_WITH_EXISTING = "style_with_existing"


class V3AvatarSource(StrEnum):
    """Avatar source types (v3-p0-contract §4.2)."""

    OOTD_PHOTO = "ootd_photo"
    OUTFIT_IMPORT = "outfit_import"
    USER_UPLOAD = "user_upload"
    CAMERA_CAPTURE = "camera_capture"


class V3AvatarPromptContext(StrEnum):
    POST_FLOW_SHEET = "post_flow_sheet"
    PREVIEW_REQUEST = "preview_request"


class V3AvatarStatus(StrEnum):
    """Avatar state machine (v3-p0-contract §4.1)."""

    NONE = "none"
    CANDIDATE = "candidate"
    PREPARING = "preparing"
    READY = "ready"
    REFRESH_RECOMMENDED = "refresh_recommended"
    DISABLED = "disabled"


class V3PreviewConcern(StrEnum):
    """Visual-preview concern enum (v3-p0-contract §6.1) — distinct from the
    decision concern enum in §5.3; the two are frozen separately and must not
    be unified."""

    VOLUME = "volume"
    COLOR = "color"
    LENGTH = "length"
    OVERALL = "overall"


class V3PreviewTerminalStatus(StrEnum):
    """Terminal `generation_status` values only (v3-p0-contract §6.2)."""

    READY = "ready"
    FAILED = "failed"
    REJECTED_BY_QUALITY_GATE = "rejected_by_quality_gate"


class V3PreviewViewResult(StrEnum):
    SHOWN = "shown"
    FALLBACK = "fallback"


class V3FeedbackVerdict(StrEnum):
    """Feedback verdicts (v3-p0-contract §6.2, verbatim from V3-020 acceptance)."""

    LOOKS_LIKE_ME = "looks_like_me"
    NOT_LIKE_ME = "not_like_me"
    GARMENT_WRONG = "garment_wrong"


class V3FeedbackAspect(StrEnum):
    FACE = "face"
    BODY_RATIO = "body_ratio"
    GARMENT_FIT = "garment_fit"
    COLOR = "color"
    OTHER = "other"


@dataclass(frozen=True)
class FieldSpec:
    """One event field: a closed, privacy-safe value space — never free text."""

    type: str
    required: bool = True
    values: tuple[str, ...] | None = None
    min: int | None = None
    max: int | None = None

    def to_contract(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"type": self.type, "required": self.required}
        if self.values is not None:
            payload["values"] = list(self.values)
        if self.type == "integer":
            payload["min"] = self.min
            payload["max"] = self.max
        return payload


@dataclass(frozen=True)
class EventSpec:
    """One event: exactly one emitting side, an explicit flow-correlation rule
    and a closed field set."""

    owner: str
    group: V3EventGroup
    flow_id: V3FlowIdRule
    fields: Mapping[str, FieldSpec]

    def to_contract(self) -> dict[str, Any]:
        return {
            "owner": self.owner,
            "group": self.group.value,
            "flow_id": self.flow_id.value,
            "fields": {name: spec.to_contract() for name, spec in self.fields.items()},
        }


@dataclass(frozen=True)
class MetricSpec:
    """One dashboard metric, defined purely in terms of events above.

    ``kill_criteria`` indexes V3_SPEC §20. A criterion with no computable
    metric is listed in :data:`QUALITATIVE_ONLY_KILL_CRITERIA` instead of being
    faked with a proxy number.
    """

    definition: str
    events: tuple[str, ...]
    fields: tuple[str, ...]
    kill_criteria: tuple[int, ...]

    def to_contract(self) -> dict[str, Any]:
        return {
            "definition": self.definition,
            "events": list(self.events),
            "fields": list(self.fields),
            "kill_criteria": list(self.kill_criteria),
        }


def _values(enum_cls: type[StrEnum]) -> tuple[str, ...]:
    return tuple(member.value for member in enum_cls)


def _enum(enum_cls: type[StrEnum], *, required: bool = True) -> FieldSpec:
    return FieldSpec(type="enum", required=required, values=_values(enum_cls))


ALLOWED_FIELD_TYPES: tuple[str, ...] = ("boolean", "enum", "enum_array", "integer", "timestamp", "uuid")

FORBIDDEN_FIELD_NAMES: frozenset[str] = frozenset(
    {
        "age_estimate",
        "attractiveness_score",
        "body_measurements",
        "body_shape_estimate",
        "canonical_front_url",
        "comment",
        "device_id",
        "email",
        "free_text",
        "height",
        "image_base64",
        "image_bytes",
        "image_url",
        "ip_address",
        "metadata",
        "model_version",
        "note",
        "phone",
        "photo_url",
        "preview_url",
        "prompt",
        "prompt_text",
        "prompt_version",
        "provider",
        "quality_signals",
        "signed_url",
        "source_url",
        "url",
        "weight",
    }
)

ENVELOPE_FIELDS: Mapping[str, FieldSpec] = {
    "schema_version": FieldSpec(type="integer", min=1, max=None),
    "occurred_at": FieldSpec(type="timestamp"),
    "platform": _enum(V3Platform),
    "user_id": FieldSpec(type="uuid"),
    "flow_id": FieldSpec(type="uuid", required=False),
}

V3_ANALYTICS_EVENTS: Mapping[V3AnalyticsEventName, EventSpec] = {
    V3AnalyticsEventName.DECISION_STARTED: EventSpec(
        owner="fe",
        group=V3EventGroup.DECISION,
        flow_id=V3FlowIdRule.REQUIRED,
        fields={"source_type": _enum(V3DecisionSource)},
    ),
    V3AnalyticsEventName.DECISION_COMPLETED: EventSpec(
        owner="be",
        group=V3EventGroup.DECISION,
        flow_id=V3FlowIdRule.REQUIRED,
        fields={
            "decision": _enum(V3Decision),
            "uncertainty": _enum(V3Uncertainty),
            "visual_need": _enum(V3VisualNeed),
            "parse_status": _enum(V3ParseStatus),
        },
    ),
    V3AnalyticsEventName.DECISION_VIEWED: EventSpec(
        owner="fe",
        group=V3EventGroup.DECISION,
        flow_id=V3FlowIdRule.REQUIRED,
        fields={"decision": _enum(V3Decision)},
    ),
    V3AnalyticsEventName.DECISION_ACTION: EventSpec(
        owner="fe",
        group=V3EventGroup.DECISION,
        flow_id=V3FlowIdRule.REQUIRED,
        fields={"action": _enum(V3DecisionAction)},
    ),
    V3AnalyticsEventName.AVATAR_PROMPT_SHOWN: EventSpec(
        owner="fe",
        group=V3EventGroup.AVATAR,
        flow_id=V3FlowIdRule.WHEN_IN_FLOW,
        fields={
            "source_type": _enum(V3AvatarSource),
            "context": _enum(V3AvatarPromptContext),
        },
    ),
    V3AnalyticsEventName.AVATAR_CONSENT: EventSpec(
        owner="be",
        group=V3EventGroup.AVATAR,
        flow_id=V3FlowIdRule.WHEN_IN_FLOW,
        fields={
            "accepted": FieldSpec(type="boolean"),
            "source_type": _enum(V3AvatarSource),
        },
    ),
    V3AnalyticsEventName.AVATAR_DELETED: EventSpec(
        owner="be",
        group=V3EventGroup.AVATAR,
        flow_id=V3FlowIdRule.NOT_APPLICABLE,
        fields={"purge_derived": FieldSpec(type="boolean")},
    ),
    V3AnalyticsEventName.PREVIEW_REQUESTED: EventSpec(
        owner="fe",
        group=V3EventGroup.PREVIEW,
        flow_id=V3FlowIdRule.REQUIRED,
        fields={
            "concern": _enum(V3PreviewConcern),
            "avatar_status": _enum(V3AvatarStatus),
        },
    ),
    V3AnalyticsEventName.PREVIEW_OUTCOME: EventSpec(
        owner="be",
        group=V3EventGroup.PREVIEW,
        flow_id=V3FlowIdRule.REQUIRED,
        fields={
            "generation_status": _enum(V3PreviewTerminalStatus),
            "cache_hit": FieldSpec(type="boolean"),
            "retry_count": FieldSpec(type="integer", min=0, max=1),
        },
    ),
    V3AnalyticsEventName.PREVIEW_VIEWED: EventSpec(
        owner="fe",
        group=V3EventGroup.PREVIEW,
        flow_id=V3FlowIdRule.REQUIRED,
        fields={"result": _enum(V3PreviewViewResult)},
    ),
    V3AnalyticsEventName.PREVIEW_FEEDBACK: EventSpec(
        owner="fe",
        group=V3EventGroup.PREVIEW,
        flow_id=V3FlowIdRule.REQUIRED,
        fields={
            "verdict": _enum(V3FeedbackVerdict),
            "aspects": FieldSpec(type="enum_array", required=False, values=_values(V3FeedbackAspect)),
        },
    ),
}

# Dashboard metrics (V3-023 input). Latency metrics are derived from envelope
# `occurred_at` deltas joined on `flow_id` — there is deliberately no duration
# field on any event, because a client-reported duration is both unverifiable
# and an open value space.
V3_ANALYTICS_METRICS: Mapping[str, MetricSpec] = {
    "decision_completion_rate": MetricSpec(
        definition=(
            "flows with v3_decision_completed / flows with v3_decision_started. "
            "The headline usefulness number; computable with every visual flag off."
        ),
        events=("v3_decision_started", "v3_decision_completed"),
        fields=("flow_id",),
        kill_criteria=(5,),
    ),
    "time_to_decision_seconds": MetricSpec(
        definition=(
            "occurred_at(v3_decision_completed) - occurred_at(v3_decision_started) per flow_id; "
            "report the median, segmented by whether the flow also emitted v3_preview_requested."
        ),
        events=("v3_decision_started", "v3_decision_completed", "v3_preview_requested"),
        fields=("occurred_at", "flow_id"),
        kill_criteria=(5,),
    ),
    "decision_action_rate": MetricSpec(
        definition=(
            "flows with v3_decision_action / flows with v3_decision_viewed. "
            "Separates a decision that changed behavior from one that was merely read."
        ),
        events=("v3_decision_viewed", "v3_decision_action"),
        fields=("flow_id",),
        kill_criteria=(3,),
    ),
    "existing_item_alternative_rate": MetricSpec(
        definition=(
            "v3_decision_action[action=style_with_existing] / all v3_decision_action. "
            "Reuse over purchase conversion (V3_SPEC §18)."
        ),
        events=("v3_decision_action",),
        fields=("action",),
        kill_criteria=(3,),
    ),
    "repeat_use_rate": MetricSpec(
        definition=(
            "distinct user_id with v3_decision_started on >= 2 distinct occurred_at dates / distinct user_id with >= 1."
        ),
        events=("v3_decision_started",),
        fields=("user_id", "occurred_at"),
        kill_criteria=(3,),
    ),
    "consent_prompt_completion_delta": MetricSpec(
        definition=(
            "decision_completion_rate over flows that emitted "
            "v3_avatar_prompt_shown[context=preview_request] minus the rate over flows that did not. "
            "A materially negative delta means photo/consent asking is costing decisions."
        ),
        events=("v3_avatar_prompt_shown", "v3_decision_started", "v3_decision_completed"),
        fields=("context", "flow_id"),
        kill_criteria=(4,),
    ),
    "consent_acceptance_rate": MetricSpec(
        definition="v3_avatar_consent[accepted=true] / all v3_avatar_consent, segmented by source_type.",
        events=("v3_avatar_consent",),
        fields=("accepted", "source_type"),
        kill_criteria=(4,),
    ),
    "preview_request_rate": MetricSpec(
        definition="flows with v3_preview_requested / flows with v3_decision_viewed.",
        events=("v3_decision_viewed", "v3_preview_requested"),
        fields=("flow_id",),
        kill_criteria=(5,),
    ),
    "preview_hidden_rate": MetricSpec(
        definition=(
            "v3_preview_outcome[generation_status in (failed, rejected_by_quality_gate)] / "
            "all v3_preview_outcome. Report the two statuses separately as well: a rising "
            "rejected share is the quality gate doing its job, a rising failed share is an outage."
        ),
        events=("v3_preview_outcome",),
        fields=("generation_status",),
        kill_criteria=(2, 9),
    ),
    "identity_fidelity_rate": MetricSpec(
        definition="v3_preview_feedback[verdict=looks_like_me] / all v3_preview_feedback.",
        events=("v3_preview_feedback",),
        fields=("verdict",),
        kill_criteria=(1,),
    ),
    "garment_fidelity_complaint_rate": MetricSpec(
        definition=(
            "v3_preview_feedback[verdict=garment_wrong or aspects contains garment_fit] / "
            "all v3_preview_feedback. A proxy for silhouette distortion, not a measurement of it."
        ),
        events=("v3_preview_feedback",),
        fields=("verdict", "aspects"),
        kill_criteria=(2,),
    ),
    "post_preview_decision_action_rate": MetricSpec(
        definition=(
            "flows with v3_decision_action after v3_preview_viewed[result=shown] / "
            "flows with v3_preview_viewed[result=shown]. Preview usefulness, not preview volume."
        ),
        events=("v3_preview_viewed", "v3_decision_action"),
        fields=("result", "flow_id", "occurred_at"),
        kill_criteria=(3,),
    ),
    "fallback_decision_retention_rate": MetricSpec(
        definition=(
            "flows with v3_decision_action among flows with v3_preview_viewed[result=fallback]. "
            "Verifies a failed or hidden preview never erases the decision."
        ),
        events=("v3_preview_viewed", "v3_decision_action"),
        fields=("result", "flow_id"),
        kill_criteria=(5,),
    ),
    "preview_cache_hit_rate": MetricSpec(
        definition="v3_preview_outcome[cache_hit=true] / all v3_preview_outcome.",
        events=("v3_preview_outcome",),
        fields=("cache_hit",),
        kill_criteria=(7,),
    ),
    "generations_per_useful_decision": MetricSpec(
        definition=(
            "count(v3_preview_outcome[cache_hit=false]) + sum(retry_count OVER THOSE SAME "
            "cache_hit=false rows), divided by "
            "count(flows with both v3_decision_completed and v3_decision_action). "
            "Paid generations per decision that actually changed behavior — the cost ratio "
            "that matters, deliberately not generations per user. The retry scope is stated "
            "explicitly because a cache hit carries the ORIGINAL asset's retry_count (see "
            "routers/visual_preview.py::_emit_cache_hit_outcome, which is correct — the field "
            "describes the asset's history): summing retry_count over ALL outcomes would "
            "re-charge one paid regeneration on every free cache read, inflating this ratio "
            "and the cost gate derived from it."
        ),
        events=("v3_preview_outcome", "v3_decision_completed", "v3_decision_action"),
        fields=("cache_hit", "retry_count", "flow_id"),
        kill_criteria=(7,),
    ),
}

# V3_SPEC §20 criteria that no event can honestly measure. Listed explicitly so
# the dashboard does not silently imply full coverage.
QUALITATIVE_ONLY_KILL_CRITERIA: Mapping[int, str] = {
    6: (
        "Users mistaking the preview for a size guarantee is a comprehension failure; "
        "only moderated sessions and support/review reports can detect it."
    ),
    8: (
        "Being clearly worse than comparable products a user has already seen is a relative "
        "judgment with no in-app referent; requires benchmark review."
    ),
    10: (
        "Sacrificing Today and import-to-first-outfit is a program-allocation judgment, "
        "not a user-behavior signal in this contract."
    ),
}

KILL_CRITERIA_COUNT = 10


def contract_dict() -> dict[str, Any]:
    """Exact structure of ``docs/v3-analytics-events.json`` (drift guard)."""
    return {
        "contract": CONTRACT_NAME,
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "flow_id_header": FLOW_ID_HEADER,
        "envelope": {"fields": {name: spec.to_contract() for name, spec in ENVELOPE_FIELDS.items()}},
        "allowed_field_types": list(ALLOWED_FIELD_TYPES),
        "forbidden_field_names": sorted(FORBIDDEN_FIELD_NAMES),
        "events": {name.value: spec.to_contract() for name, spec in V3_ANALYTICS_EVENTS.items()},
        "metrics": {name: spec.to_contract() for name, spec in V3_ANALYTICS_METRICS.items()},
        "qualitative_only_kill_criteria": {
            str(number): reason for number, reason in sorted(QUALITATIVE_ONLY_KILL_CRITERIA.items())
        },
    }


def resolve_flow_id(correlation_id: str | None) -> str | None:
    """Map the request correlation id onto the envelope ``flow_id``.

    FE generates a UUIDv4 per decision flow and sends it as ``X-Correlation-Id``
    (see ``docs/v3-analytics-events.md`` §3), which the existing
    ``CorrelationIdMiddleware`` already accepts and propagates — so flow
    correlation costs no API-shape change. A caller that sends a valid but
    non-UUID correlation id still gets a stable per-flow identifier (derived,
    not random) so the envelope's uuid type holds and BE events from one request
    chain still group together.
    """
    if not correlation_id:
        return None
    try:
        return str(uuid.UUID(correlation_id))
    except ValueError:
        return str(uuid.uuid5(_FLOW_ID_NAMESPACE, correlation_id))


def validate_v3_event(event_name: str, fields: Mapping[str, object], *, flow_id: str | None = None) -> None:
    """Validate one V3 analytics event payload against the frozen contract.

    Emitters added by later V3 features must call this before dispatch so a
    payload can never drift from the contract or carry a forbidden field.
    Raises ``ValueError`` on any violation.
    """
    try:
        spec = V3_ANALYTICS_EVENTS[V3AnalyticsEventName(event_name)]
    except ValueError as exc:
        raise ValueError(f"unknown V3 analytics event: {event_name}") from exc
    if spec.flow_id is V3FlowIdRule.REQUIRED and flow_id is None:
        raise ValueError(f"{event_name}: flow_id is required")
    if spec.flow_id is V3FlowIdRule.NOT_APPLICABLE and flow_id is not None:
        raise ValueError(f"{event_name}: flow_id does not apply to this event")
    for name in fields:
        if name in FORBIDDEN_FIELD_NAMES:
            raise ValueError(f"{event_name}: field '{name}' is forbidden by the privacy contract")
        if name not in spec.fields:
            raise ValueError(f"{event_name}: unknown field '{name}'")
    for name, field_spec in spec.fields.items():
        if name in fields:
            _validate_value(event_name, name, field_spec, fields[name])
        elif field_spec.required:
            raise ValueError(f"{event_name}: missing required field '{name}'")


def _validate_value(event_name: str, name: str, spec: FieldSpec, value: object) -> None:
    label = f"{event_name}.{name}"
    if spec.type == "boolean":
        if not isinstance(value, bool):
            raise ValueError(f"{label}: expected boolean")
    elif spec.type == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{label}: expected integer")
        if spec.min is not None and value < spec.min:
            raise ValueError(f"{label}: below minimum {spec.min}")
        if spec.max is not None and value > spec.max:
            raise ValueError(f"{label}: above maximum {spec.max}")
    elif spec.type == "enum":
        if not isinstance(value, str) or spec.values is None or value not in spec.values:
            raise ValueError(f"{label}: expected one of {spec.values}")
    elif spec.type == "enum_array":
        if not isinstance(value, Sequence) or isinstance(value, str):
            raise ValueError(f"{label}: expected a list")
        for item in value:
            if not isinstance(item, str) or spec.values is None or item not in spec.values:
                raise ValueError(f"{label}: expected items from {spec.values}")
    else:  # uuid / timestamp — envelope-only in P0, opaque strings here
        if not isinstance(value, str) or not value:
            raise ValueError(f"{label}: expected a non-empty string")
