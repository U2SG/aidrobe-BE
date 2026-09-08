"""Authenticated same-origin relay, disabled independently of product flags.

Read the bounded body manually: the shared Pydantic error handler logs rejected
input values, which is not acceptable at an analytics privacy boundary.
"""

import json
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse

from app.common.analytics_delivery import publish_records, validate_record
from app.config.settings import get_settings
from app.infrastructure.persistence.models.user import User
from app.presentation.api.v1.deps import get_current_user


def _gate() -> None:
    if not get_settings().ENABLE_V3_ANALYTICS:
        raise HTTPException(503, "Telemetry disabled", headers={"X-Aidrobe-Telemetry-Disabled": "1"})


router = APIRouter(prefix="/v1/analytics", tags=["analytics"], dependencies=[Depends(_gate)])
_REQUEST_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["events"],
    "properties": {
        "events": {
            "type": "array",
            "minItems": 1,
            "maxItems": 8,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["event_id", "event_name", "properties"],
                "properties": {
                    "event_id": {"type": "string", "format": "uuid"},
                    "event_name": {"type": "string", "description": "FE-owned name in the frozen V3 registry."},
                    "properties": {
                        "type": "object",
                        "description": "Frozen V3 envelope + per-event allowlist; runtime validated.",
                    },
                },
            },
        }
    },
}


@router.post(
    "/events",
    status_code=202,
    responses={
        202: {"description": "Accepted into bounded memory; not a durability acknowledgement."},
        413: {"description": "Body exceeds 16 KiB."},
        422: {"description": "Invalid event, ownership, identity, envelope or payload; input is never echoed."},
        503: {"description": "Telemetry disabled or queue unavailable/full."},
    },
    openapi_extra={"requestBody": {"required": True, "content": {"application/json": {"schema": _REQUEST_SCHEMA}}}},
)
async def ingest_events(
    request: Request,
    current_user: Annotated[User, Depends(get_current_user)],
) -> JSONResponse:
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 16_384:
            raise HTTPException(413, "Analytics body too large")
        raw.extend(chunk)
    try:
        body = json.loads(raw)
        if not isinstance(body, dict) or set(body) != {"events"}:
            raise ValueError
        events = body["events"]
        if not isinstance(events, list) or not 1 <= len(events) <= 8:
            raise ValueError
        records = [validate_record(event, owner="fe", user_id=current_user.id) for event in events]
    except (ValueError, TypeError, UnicodeError, RecursionError):
        raise HTTPException(422, "Invalid analytics batch") from None
    # Validate the entire batch first; a forbidden field never partly enters.
    if not publish_records(records):
        _gate()
        raise HTTPException(503, "Analytics queue unavailable")
    return JSONResponse(status_code=202, content={"accepted": True})
