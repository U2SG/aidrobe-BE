"""Validated, vendor-neutral V3 delivery port (FE #157); no product vocabulary here."""

from __future__ import annotations

import json
import re
import uuid
from collections.abc import Callable, Sequence
from datetime import datetime
from typing import Literal, TypedDict, cast

from app.common.analytics_events import (
    ENVELOPE_FIELDS,
    V3_ANALYTICS_EVENTS,
    V3AnalyticsEventName,
    validate_v3_event,
)


class AnalyticsRecord(TypedDict):
    # Delivery identity is not a new analytics event property.
    event_id: str
    event_name: str
    properties: dict[str, object]


Sink = Callable[[Sequence[AnalyticsRecord]], bool]
_sink: Sink | None = None
_TIMESTAMP = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$")


def _uuid(value: object) -> bool:
    try:
        return isinstance(value, str) and str(uuid.UUID(value)) == value
    except ValueError:
        return False


def validate_record(
    raw: object,
    *,
    owner: Literal["fe", "be"],
    user_id: uuid.UUID | None = None,
) -> AnalyticsRecord:
    """Reject unknown/nested/free-text fields before any logger or sink sees them.

    Errors deliberately contain no input values, field names or event names.
    """
    invalid = ValueError("Invalid analytics record")
    if not isinstance(raw, dict) or set(raw) != {"event_id", "event_name", "properties"}:
        raise invalid
    if not _uuid(raw["event_id"]) or not isinstance(raw["event_name"], str):
        raise invalid
    try:
        name = V3AnalyticsEventName(raw["event_name"])
    except ValueError:
        raise invalid from None
    spec = V3_ANALYTICS_EVENTS[name]
    props = raw["properties"]
    if spec.owner != owner or not isinstance(props, dict):
        raise invalid
    if type(props.get("schema_version")) is not int or props["schema_version"] != 1:
        raise invalid
    if not _uuid(props.get("user_id")) or (user_id is not None and props["user_id"] != str(user_id)):
        raise invalid
    if props.get("platform") not in (("ios", "android") if owner == "fe" else ("backend",)):
        raise invalid
    when = props.get("occurred_at")
    if not isinstance(when, str) or len(when) > 40 or not _TIMESTAMP.fullmatch(when):
        raise invalid
    try:
        if datetime.fromisoformat(when).tzinfo is None:
            raise invalid
    except ValueError:
        raise invalid from None
    flow = props.get("flow_id")
    if "flow_id" in props and not _uuid(flow):
        raise invalid
    fields = {key: value for key, value in props.items() if key not in ENVELOPE_FIELDS}
    if any(isinstance(value, list) and len(value) > 5 for value in fields.values()):
        raise invalid
    try:
        validate_v3_event(name, fields, flow_id=cast(str | None, flow))
        serialized = json.dumps(raw, ensure_ascii=True, allow_nan=False)
        if len(serialized) > 2300:
            raise invalid
        return cast(AnalyticsRecord, json.loads(serialized))
    except (ValueError, TypeError):
        raise invalid from None


def set_analytics_sink(sink: Sink | None) -> None:
    global _sink
    _sink = sink


def publish_records(records: Sequence[AnalyticsRecord]) -> bool:
    """Best-effort enqueue only; never turn analytics into a product failure."""
    try:
        return _sink(records) if _sink is not None else False
    except Exception:
        return False
