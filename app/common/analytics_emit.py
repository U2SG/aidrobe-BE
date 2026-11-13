"""BE-side validated V3 events through the API/worker delivery port.

No event properties are copied into general application logs: the independently
disabled collector must not leave a second uncontrolled analytics copy.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime

import structlog

from app.common.analytics_delivery import publish_records, validate_record
from app.common.analytics_events import CONTRACT_SCHEMA_VERSION, validate_v3_event

logger = structlog.get_logger("v3_analytics")

_PLATFORM_BACKEND = "backend"


def _warn_invalid_event() -> None:
    try:
        logger.warning("v3_analytics_event_invalid")
    except Exception:
        pass  # Even diagnostic logging must not break a business request.


def emit_v3_event(
    event_name: str,
    fields: Mapping[str, object],
    *,
    user_id: uuid.UUID,
    flow_id: str | None = None,
) -> bool:
    """Validate one event against the frozen contract, then emit it.

    True means a valid event, not durable delivery. The default-off API/worker
    port handles best-effort export independently.

    Returns ``False`` (with a field-free warning — event payloads must never
    leak through an error path) instead of raising: analytics must never fail
    the request that produced them.
    """
    try:
        validate_v3_event(event_name, fields, flow_id=flow_id)
    except (ValueError, TypeError):
        _warn_invalid_event()
        return False
    envelope: dict[str, object] = {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "occurred_at": datetime.now(UTC).isoformat(),
        "platform": _PLATFORM_BACKEND,
        "user_id": str(user_id),
    }
    if flow_id is not None:
        envelope["flow_id"] = flow_id
    try:
        record = validate_record(
            {"event_id": str(uuid.uuid4()), "event_name": event_name, "properties": {**envelope, **dict(fields)}},
            owner="be", user_id=user_id,
        )
    except (ValueError, TypeError):
        _warn_invalid_event()
        return False
    # No payload fallback to application logs when the port is disabled/down.
    publish_records([record])
    return True
