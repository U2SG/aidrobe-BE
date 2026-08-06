"""V3-002 — the analytics event contract is the same artifact on both sides.

These tests are the BE half of the FE/BE mapping evidence; the FE half is
``scripts/v3AnalyticsEvents.test.ts`` in aidrobe-FE, which asserts the TS mirror
against a byte-identical copy of the same JSON file.
"""

import json
import uuid

import pytest

from app.common.analytics_events import (
    ALLOWED_FIELD_TYPES,
    CONTRACT_PATH,
    ENVELOPE_FIELDS,
    FORBIDDEN_FIELD_NAMES,
    KILL_CRITERIA_COUNT,
    QUALITATIVE_ONLY_KILL_CRITERIA,
    V3_ANALYTICS_EVENTS,
    V3_ANALYTICS_METRICS,
    V3AnalyticsEventName,
    V3EventGroup,
    V3FlowIdRule,
    contract_dict,
    resolve_flow_id,
    validate_v3_event,
)

FLOW_ID = "7f1d1f6c-1a3e-4c1e-9f0a-2b6d5e4c3a21"


def test_json_artifact_matches_the_module() -> None:
    """The checked-in artifact is generated from this module; regenerate with
    `uv run python -c "import json; from app.common.analytics_events import
    contract_dict; ..."` (see docs/v3-analytics-events.md §8)."""
    on_disk = json.loads(CONTRACT_PATH.read_text(encoding="utf-8"))
    assert on_disk == contract_dict()


def test_every_event_name_has_exactly_one_spec() -> None:
    assert set(V3_ANALYTICS_EVENTS) == set(V3AnalyticsEventName)
    for name, spec in V3_ANALYTICS_EVENTS.items():
        assert spec.owner in {"fe", "be"}, name


# --- privacy field review (acceptance 2) -------------------------------------
# The declared field set is what makes the contract privacy-safe; a runtime
# rejection in validate_v3_event only protects a correct caller.


def test_no_declared_field_is_a_forbidden_name() -> None:
    for name, spec in V3_ANALYTICS_EVENTS.items():
        for field_name in spec.fields:
            assert field_name not in FORBIDDEN_FIELD_NAMES, f"{name}.{field_name}"
    for field_name in ENVELOPE_FIELDS:
        assert field_name not in FORBIDDEN_FIELD_NAMES


def test_every_declared_field_is_a_closed_value_space() -> None:
    """No free text anywhere: enums enumerate their values, integers are bounded,
    and the only opaque strings are the envelope's uuid/timestamp fields."""
    for name, spec in V3_ANALYTICS_EVENTS.items():
        for field_name, field_spec in spec.fields.items():
            label = f"{name}.{field_name}"
            assert field_spec.type in ALLOWED_FIELD_TYPES, label
            assert field_spec.type not in {"uuid", "timestamp"}, f"{label}: opaque strings are envelope-only"
            if field_spec.type in {"enum", "enum_array"}:
                assert field_spec.values, label
            if field_spec.type == "integer":
                assert field_spec.min is not None and field_spec.max is not None, label


def test_validate_rejects_a_forbidden_field_with_a_privacy_error() -> None:
    with pytest.raises(ValueError, match="forbidden by the privacy contract"):
        validate_v3_event(
            "v3_preview_viewed",
            {"result": "shown", "image_url": "https://example.test/x.jpg"},
            flow_id=FLOW_ID,
        )


# --- decision value vs visual-generation usage (acceptance 1) ----------------


def test_decision_metrics_are_computable_without_any_preview_event() -> None:
    decision_only = {
        "decision_completion_rate",
        "decision_action_rate",
        "existing_item_alternative_rate",
        "repeat_use_rate",
    }
    for metric_name in decision_only:
        for event_name in V3_ANALYTICS_METRICS[metric_name].events:
            group = V3_ANALYTICS_EVENTS[V3AnalyticsEventName(event_name)].group
            assert group is V3EventGroup.DECISION, f"{metric_name} depends on {event_name} ({group})"


def test_no_metric_counts_generation_volume_alone() -> None:
    """Cost is reported per useful decision, never as a generation count."""
    cost_metric = V3_ANALYTICS_METRICS["generations_per_useful_decision"]
    assert "v3_decision_action" in cost_metric.events


# --- metrics resolve against the event contract (acceptance 4) ---------------


def test_every_metric_reference_resolves_to_a_real_event_field() -> None:
    envelope = set(ENVELOPE_FIELDS)
    for metric_name, metric in V3_ANALYTICS_METRICS.items():
        assert metric.events, metric_name
        known_fields = set(envelope)
        for event_name in metric.events:
            spec = V3_ANALYTICS_EVENTS[V3AnalyticsEventName(event_name)]
            known_fields |= set(spec.fields)
        for field_name in metric.fields:
            assert field_name in known_fields, f"{metric_name} references unknown field {field_name}"


def test_kill_criteria_are_covered_exactly_once_by_metrics_or_declared_qualitative() -> None:
    computable = {number for metric in V3_ANALYTICS_METRICS.values() for number in metric.kill_criteria}
    qualitative = set(QUALITATIVE_ONLY_KILL_CRITERIA)
    assert not computable & qualitative, "a criterion cannot be both computable and qualitative-only"
    assert computable | qualitative == set(range(1, KILL_CRITERIA_COUNT + 1))


# --- flow correlation --------------------------------------------------------


def test_flow_id_is_required_for_every_decision_and_preview_event() -> None:
    for name, spec in V3_ANALYTICS_EVENTS.items():
        if spec.group in {V3EventGroup.DECISION, V3EventGroup.PREVIEW}:
            assert spec.flow_id is V3FlowIdRule.REQUIRED, name


def test_avatar_prompt_may_carry_a_flow_id_so_criterion_4_stays_computable() -> None:
    spec = V3_ANALYTICS_EVENTS[V3AnalyticsEventName.AVATAR_PROMPT_SHOWN]
    assert spec.flow_id is V3FlowIdRule.WHEN_IN_FLOW
    payload = {"source_type": "user_upload", "context": "preview_request"}
    validate_v3_event("v3_avatar_prompt_shown", payload, flow_id=FLOW_ID)
    validate_v3_event("v3_avatar_prompt_shown", payload, flow_id=None)


def test_missing_or_inapplicable_flow_id_is_rejected() -> None:
    with pytest.raises(ValueError, match="flow_id is required"):
        validate_v3_event("v3_decision_viewed", {"decision": "wait"})
    with pytest.raises(ValueError, match="flow_id does not apply"):
        validate_v3_event("v3_avatar_deleted", {"purge_derived": True}, flow_id=FLOW_ID)


def test_resolve_flow_id_passes_uuids_through_and_derives_the_rest() -> None:
    assert resolve_flow_id(None) is None
    assert resolve_flow_id("") is None
    assert resolve_flow_id(FLOW_ID) == FLOW_ID
    assert resolve_flow_id(FLOW_ID.upper()) == FLOW_ID  # normalized, not duplicated

    derived = resolve_flow_id("req-42")
    assert derived is not None
    assert uuid.UUID(derived)  # satisfies the envelope's uuid type
    assert derived == resolve_flow_id("req-42")  # stable, so a request chain groups
    assert derived != resolve_flow_id("req-43")


# --- payload validation ------------------------------------------------------


def test_valid_payloads_pass_for_every_event() -> None:
    samples: dict[str, dict[str, object]] = {
        "v3_decision_started": {"source_type": "share_link"},
        "v3_decision_completed": {
            "decision": "avoid_duplicate",
            "uncertainty": "low",
            "visual_need": "none",
            "parse_status": "parsed",
        },
        "v3_decision_viewed": {"decision": "worth_adding"},
        "v3_decision_action": {"action": "style_with_existing"},
        "v3_avatar_prompt_shown": {"source_type": "ootd_photo", "context": "post_flow_sheet"},
        "v3_avatar_consent": {"accepted": False, "source_type": "camera_capture"},
        "v3_avatar_deleted": {"purge_derived": True},
        "v3_preview_requested": {"concern": "volume", "avatar_status": "ready"},
        "v3_preview_outcome": {
            "generation_status": "rejected_by_quality_gate",
            "cache_hit": False,
            "retry_count": 1,
        },
        "v3_preview_viewed": {"result": "fallback"},
        "v3_preview_feedback": {"verdict": "garment_wrong", "aspects": ["garment_fit", "color"]},
    }
    assert set(samples) == {name.value for name in V3AnalyticsEventName}
    for event_name, payload in samples.items():
        spec = V3_ANALYTICS_EVENTS[V3AnalyticsEventName(event_name)]
        flow_id = None if spec.flow_id is V3FlowIdRule.NOT_APPLICABLE else FLOW_ID
        validate_v3_event(event_name, payload, flow_id=flow_id)


def test_optional_fields_may_be_omitted() -> None:
    validate_v3_event("v3_preview_feedback", {"verdict": "looks_like_me"}, flow_id=FLOW_ID)


@pytest.mark.parametrize(
    ("event_name", "payload", "message"),
    [
        ("v3_nope", {}, "unknown V3 analytics event"),
        ("v3_decision_viewed", {}, "missing required field 'decision'"),
        ("v3_decision_viewed", {"decision": "maybe"}, "expected one of"),
        ("v3_decision_viewed", {"decision": "wait", "extra": 1}, "unknown field 'extra'"),
        ("v3_avatar_consent", {"accepted": "yes", "source_type": "user_upload"}, "expected boolean"),
        (
            "v3_preview_outcome",
            {"generation_status": "ready", "cache_hit": True, "retry_count": 2},
            "above maximum 1",
        ),
        (
            "v3_preview_outcome",
            {"generation_status": "ready", "cache_hit": True, "retry_count": True},
            "expected integer",
        ),
        (
            "v3_preview_feedback",
            {"verdict": "looks_like_me", "aspects": "face"},
            "expected a list",
        ),
        (
            "v3_preview_feedback",
            {"verdict": "looks_like_me", "aspects": ["cheekbones"]},
            "expected items from",
        ),
    ],
)
def test_invalid_payloads_are_rejected(event_name: str, payload: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        validate_v3_event(event_name, payload, flow_id=FLOW_ID)
