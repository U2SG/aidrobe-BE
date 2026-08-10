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
