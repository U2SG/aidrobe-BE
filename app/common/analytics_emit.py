"""BE-side V3 analytics emission — the structured-logger sink V3-002 chose.

The contract (`docs/v3-analytics-events.md` §0) keeps the backend on its
structured logger until a real sink is picked post-P0, and requires every
emitter to validate against the frozen registry first. This module is that
single seam: features never call the logger directly for V3 events.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from datetime import UTC, datetime

import structlog

from app.common.analytics_events import CONTRACT_SCHEMA_VERSION, validate_v3_event

logger = structlog.get_logger("v3_analytics")

_PLATFORM_BACKEND = "backend"


def emit_v3_event(
    event_name: str,
    fields: Mapping[str, object],
    *,
    user_id: uuid.UUID,
    flow_id: str | None = None,
) -> bool:
    """Validate one event against the frozen contract, then emit it.

    Returns ``False`` (with a field-free warning — event payloads must never
    leak through an error path) instead of raising: analytics must never fail
    the request that produced them.
    """
    try:
        validate_v3_event(event_name, fields, flow_id=flow_id)
    except ValueError as exc:
        logger.warning("v3_analytics_event_invalid", event_name=event_name, reason=str(exc))
        return False
    envelope: dict[str, object] = {
        "schema_version": CONTRACT_SCHEMA_VERSION,
        "occurred_at": datetime.now(UTC).isoformat(),
        "platform": _PLATFORM_BACKEND,
        "user_id": str(user_id),
    }
    if flow_id is not None:
        envelope["flow_id"] = flow_id
    logger.info(event_name, **envelope, **dict(fields))
    return True
