"""V3-024 — `POST /v1/outfit-visual-assets/{asset_id}/feedback` (contract §6.1).

The last unowned endpoint in §6.1. FE PR #149 has been calling it since V3-020
and receiving 404 from a route that did not exist; this module registers it.
Like `visual_preview`, it owns exactly three things — the frozen request/
response shapes, the flag gate, and the mapping from the use case's outcomes
onto contract §3's envelope. Everything that decides whether a verdict may be
recorded is in `RecordPreviewFeedback`.

**Why this is a second router and not a handler on `outfit_visual`.**
`GET /v1/outfit-visual-assets/{asset_id}` shares the prefix but must stay
**ungated** — contract §2 gates the *new* preview surfaces only, and V2's
outfit-board polling depends on that GET no matter what `ENABLE_V3_STATIC_TRYON`
says. A router-level dependency is per-router, so putting the gated POST on the
M10 router would either gate the GET too or push the gate into the handler body,
where a flag-off request would be validated (and its 422 would leak the request
schema of a surface that is supposed to not exist yet). Two routers on one
prefix is the `purchase_candidates` precedent.

`_gate` is duplicated from `visual_preview` rather than shared, for one
concrete reason: the flag is read through this module's own `get_settings`,
which is the seam every V3 API test monkeypatches (`get_settings` is
`lru_cache`d, so `app.dependency_overrides` does not reach it —
tests/api/test_v3_purchase_decision_endpoint.py established the convention).
Hoisting it into `deps.py` would silently move that seam for V3-019's passing
tests to buy six lines.

**404, three ways, one answer.** Missing asset, someone else's asset, and the
caller's own non-`static_tryon` asset all answer 404 with the same body. That
is contract §3's no-existence-leak rule plus BE#154's "do not let this become a
generic image-feedback endpoint"; the conjunction lives in the use case's WHERE
clause, so this module cannot get it wrong.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.purchase.record_preview_feedback import (
    FeedbackTargetNotFound,
    RecordPreviewFeedback,
)
from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.config.settings import get_settings
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.deps import get_current_user
from app.presentation.api.v1.schemas.visual_preview import (
    PreviewFeedbackRequest,
    PreviewFeedbackResponse,
)


def _gate() -> None:
    if not get_settings().ENABLE_V3_STATIC_TRYON:
        raise FriendlyException(
            error_codes.FORBIDDEN,
            "V3 static try-on is not enabled",
            status_code=503,
        )


router = APIRouter(
    prefix="/v1/outfit-visual-assets",
    tags=["visual-preview"],
    dependencies=[Depends(_gate)],
)

_ERRORS: dict[int | str, dict[str, str]] = {
    404: {
        "description": (
            "No static try-on render with this id belongs to this user. "
            "A foreign asset, a missing asset and the caller's own non-try-on "
            "asset are indistinguishable here (contract §3)."
        )
    },
    422: {"description": "Verdict or aspect outside the frozen vocabulary, or an extra field."},
    503: {"description": "`ENABLE_V3_STATIC_TRYON` is off."},
}


@router.post(
    "/{asset_id}/feedback",
    response_model=PreviewFeedbackResponse,
    responses=_ERRORS,
)
async def record_preview_feedback(
    asset_id: uuid.UUID,
    payload: PreviewFeedbackRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> PreviewFeedbackResponse:
    # 200 on every accepted submission, including a repeat: the first tap and
    # an identical re-tap describe the same recorded state, and a 201/200 split
    # would make the client's retry look like a different outcome when it is
    # not. The V3-007 reasoning for the decision endpoint, same shape.
    try:
        recorded = await RecordPreviewFeedback(session).execute(
            user_id=current_user.id,
            asset_id=asset_id,
            verdict=payload.verdict,
            aspects=payload.aspects,
        )
    except FeedbackTargetNotFound as missing:
        raise FriendlyException(
            error_codes.NOT_FOUND, "Asset not found", status_code=404
        ) from missing

    return PreviewFeedbackResponse(
        asset_id=asset_id,
        # The *stored* verdict, not the submitted one. They are equal under
        # last-write-wins, and echoing what the database holds keeps that true
        # by construction rather than by assumption.
        verdict=recorded.verdict,
        aspects=list(recorded.aspects) if recorded.aspects is not None else None,
        recorded_at=recorded.recorded_at,
    )
