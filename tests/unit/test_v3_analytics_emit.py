"""Codex review, BE#143 — the backend `v3_decision_completed` emitter.

The contract validation itself is covered by `test_v3_analytics_events.py`;
these tests pin the emission seam: a valid payload emits, an invalid one is
swallowed (analytics must never fail the request), and the router helper only
joins flows through a real UUID correlation id.

Runs under `pytest --noconftest`.
"""

from __future__ import annotations

import uuid

import structlog

from app.common.analytics_emit import emit_v3_event

_VALID_FIELDS = {
    "decision": "worth_adding",
    "uncertainty": "low",
    "visual_need": "none",
    "parse_status": "parsed",
}


def test_valid_decision_completed_emits() -> None:
    assert (
        emit_v3_event(
            "v3_decision_completed",
            _VALID_FIELDS,
            user_id=uuid.uuid4(),
            flow_id=str(uuid.uuid4()),
        )
        is True
    )


def test_missing_flow_id_is_swallowed_not_raised() -> None:
    # flow_id is REQUIRED for this event; the emitter reports failure instead
    # of raising into the decision request.
    assert emit_v3_event("v3_decision_completed", _VALID_FIELDS, user_id=uuid.uuid4()) is False


def test_out_of_vocabulary_value_is_swallowed() -> None:
    fields = dict(_VALID_FIELDS, decision="buy_it_now")
    assert (
        emit_v3_event(
            "v3_decision_completed", fields, user_id=uuid.uuid4(), flow_id=str(uuid.uuid4())
        )
        is False
    )


def test_router_helper_requires_a_uuid_correlation_id(monkeypatch) -> None:
    from app.presentation.api.v1.routers import purchase_candidates as router

    calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        router, "emit_v3_event", lambda name, fields, **kw: calls.append({"name": name, **kw})
    )

    class _Candidate:
        status = "parsed"

    class _Decision:
        decision = "worth_adding"
        uncertainty = "low"
        visual_need = "none"

    # A non-UUID correlation id cannot join a flow — no emit, no fabricated id.
    structlog.contextvars.bind_contextvars(correlation_id="not-a-uuid")
    try:
        router._emit_decision_completed(uuid.uuid4(), _Candidate(), _Decision())
        assert calls == []

        flow = str(uuid.uuid4())
        structlog.contextvars.bind_contextvars(correlation_id=flow)
        router._emit_decision_completed(uuid.uuid4(), _Candidate(), _Decision())
        assert len(calls) == 1
        assert calls[0]["flow_id"] == flow
    finally:
        structlog.contextvars.unbind_contextvars("correlation_id")


# --- V3-013: the backend avatar emitters ----------------------------------
#
# The analytics contract (§0) assigns these to this feature by name: "emitters
# ship with the feature that owns the surface (V3-008 decision, V3-013 avatar,
# V3-019/V3-020 preview)". Raised by Codex review on BE#147 as two P1s; the
# events were contracted by V3-002 and had no emitter until now.


def test_valid_avatar_consent_emits_with_and_without_a_flow() -> None:
    """`when_in_flow`, so BOTH forms are valid — and both must be.

    A consent reached from a preview flow carries the flow; one reached from
    settings does not, and suppressing it would undercount exactly the consents
    that happen outside the funnel.
    """
    fields = {"accepted": True, "source_type": "ootd_photo"}
    assert emit_v3_event("v3_avatar_consent", fields, user_id=uuid.uuid4()) is True
    assert (
        emit_v3_event(
            "v3_avatar_consent", fields, user_id=uuid.uuid4(), flow_id=str(uuid.uuid4())
        )
        is True
    )


def test_a_declined_consent_is_a_valid_event() -> None:
    """`accepted` is a field of the event, not a filter on it."""
    assert (
        emit_v3_event(
            "v3_avatar_consent",
            {"accepted": False, "source_type": "user_upload"},
            user_id=uuid.uuid4(),
        )
        is True
    )


def test_avatar_consent_rejects_a_source_type_outside_the_frozen_vocabulary() -> None:
    assert (
        emit_v3_event(
            "v3_avatar_consent",
            {"accepted": True, "source_type": "screenshot"},
            user_id=uuid.uuid4(),
        )
        is False
    )


def test_valid_avatar_deleted_emits() -> None:
    assert (
        emit_v3_event("v3_avatar_deleted", {"purge_derived": True}, user_id=uuid.uuid4())
        is True
    )


def test_a_flow_id_on_avatar_deleted_is_swallowed_not_raised() -> None:
    """`not_applicable`: deletion is a settings action, not a funnel step.

    Worth pinning because the failure is silent — `emit_v3_event` returns False
    and logs a warning, so getting the requiredness backwards would delete the
    event rather than the request, and nobody would notice until the dashboard
    was empty.
    """
    assert (
        emit_v3_event(
            "v3_avatar_deleted",
            {"purge_derived": False},
            user_id=uuid.uuid4(),
            flow_id=str(uuid.uuid4()),
        )
        is False
    )


def test_the_avatar_router_flow_id_helper_only_joins_a_real_uuid() -> None:
    from app.presentation.api.v1.routers import avatar as router

    structlog.contextvars.bind_contextvars(correlation_id="not-a-uuid")
    try:
        # None rather than a fabricated id: a made-up flow corrupts every join.
        assert router._flow_id() is None
        flow = str(uuid.uuid4())
        structlog.contextvars.bind_contextvars(correlation_id=flow)
        assert router._flow_id() == flow
    finally:
        structlog.contextvars.unbind_contextvars("correlation_id")
