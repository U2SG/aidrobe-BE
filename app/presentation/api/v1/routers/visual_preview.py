"""V3-019 — the static visual preview create surface (contract §6.1).

``POST /v1/wardrobe-decisions/{decision_id}/visual-preview`` is the route
V3-017 built the pipeline for and deliberately did not register. Everything
that decides whether a preview is legal and renderable lives in
``RequestStaticPreview``; this module owns exactly three things — the frozen
request/response shapes, the flag gate, and the mapping from the use case's
closed ``PreviewRefusal`` vocabulary onto contract §3's error envelope.

Gated by ``ENABLE_V3_STATIC_TRYON``, default off → 503 (contract §2: the flag
gates the NEW preview surfaces only; the existing M10
``GET /v1/outfit-visual-assets/{asset_id}`` stays ungated no matter what this
flag says, because V2 outfit-board polling depends on it). Router-level rather
than a first line in the handler, the ``purchase_candidates`` reasoning: a
flag-off request must 503 without its body being inspected, or a 422 would
leak the request schema of a surface that is supposed to not exist yet.

**The refusal mapping, and why two different kinds of 4091 exist.**

Contract §6.1 freezes exactly one refusal on the wire: avatar not
``ready``/``refresh_recommended`` ⇒ ``4091 STATE_CONFLICT``. The use case can
also refuse because the candidate is a link (no garment image exists anywhere)
or because the decision snapshot composed no grounded outfit. Those are the
same HTTP shape — the decision exists, its state does not permit a preview —
but they are not the same *user* situation: an avatar problem is actionable
(build or refresh the reference and come back), while a link-only candidate
can never be rendered no matter what the user does, and V3-017's handoff asks
V3-019 to surface that as a "no preview possible" state rather than an error.
The envelope's existing ``data`` field carries ``{"reason": …}`` so FE can
tell them apart; a client that predates the field falls back on the generic
4091 treatment, which degrades safely (§3's version-skew direction). No new
top-level error code — §3 forbids that.

``DECISION_NOT_FOUND`` is 404 whether the decision is missing or someone
else's (§3, no existence leak) — the ownership check is inside the use case's
WHERE clause, so this module cannot get it wrong.

**The BE half of ``v3_preview_outcome`` for cache hits.** The analytics
contract says the event fires once per generation, at the terminal state; the
worker owns that for renders it finishes. But a flow whose request is answered
from cache never touches the worker — the outcome it observed is the cached
asset's, and ``cache_hit: true`` is the field that exists to say so. Emitted
only when the served asset is already terminal AND this request started no
work: a cache hit on a still-pending asset belongs to the generation that is
still running, and the worker will emit that one.
"""

import uuid
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports.static_tryon import StaticTryOnPort
from app.application.purchase.request_static_preview import (
    PreviewRefusal,
    PreviewRefused,
    PreviewRequested,
    RequestStaticPreview,
)
from app.bootstrap.container import static_tryon
from app.common import error_codes
from app.common.analytics_emit import emit_v3_event
from app.common.analytics_events import V3AnalyticsEventName, resolve_flow_id
from app.common.exceptions import FriendlyException
from app.config.settings import get_settings
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.deps import get_current_user
from app.presentation.api.v1.schemas.visual_preview import (
    VisualPreviewRequest,
    VisualPreviewResponse,
)

logger = structlog.get_logger(__name__)

#: Contract §6.2's terminal states — the only ones a cache hit can report as
#: an outcome. Deliberately NOT imported from the worker: presentation must
#: not import from infrastructure.workers (layer rule), and the vocabulary is
#: frozen in the contract, not owned by the worker.
_TERMINAL_STATUSES = ("ready", "failed", "rejected_by_quality_gate")


def _gate() -> None:
    if not get_settings().ENABLE_V3_STATIC_TRYON:
        raise FriendlyException(
            error_codes.FORBIDDEN,
            "V3 static try-on is not enabled",
            status_code=503,
        )


router = APIRouter(
    prefix="/v1/wardrobe-decisions",
    tags=["visual-preview"],
    dependencies=[Depends(_gate)],
)


def get_static_tryon() -> StaticTryOnPort:
    return static_tryon()


#: PreviewRefusal → (error code, HTTP status, message, data). One closed
#: vocabulary in, one closed table out — a refusal added to the use case
#: without a row here fails the exhaustiveness test rather than answering 500.
_REFUSAL_RESPONSES: dict[PreviewRefusal, tuple[int, int, str, dict[str, str] | None]] = {
    # Unreachable through this router (the gate raises first) but mapped so the
    # table is total over the enum and the use case stays router-agnostic.
    PreviewRefusal.FEATURE_DISABLED: (
        error_codes.FORBIDDEN,
        503,
        "V3 static try-on is not enabled",
        None,
    ),
    PreviewRefusal.DECISION_NOT_FOUND: (
        error_codes.NOT_FOUND,
        404,
        "Decision not found",
        None,
    ),
    # Contract §6.1, verbatim.
    PreviewRefusal.AVATAR_NOT_READY: (
        error_codes.STATE_CONFLICT,
        409,
        "Avatar reference is not ready",
        {"reason": "avatar_not_ready"},
    ),
    # The two "no preview possible" states — same wire shape as above, told
    # apart by data.reason (module docstring).
    PreviewRefusal.NO_RENDERABLE_CANDIDATE: (
        error_codes.STATE_CONFLICT,
        409,
        "This candidate has no image to render",
        {"reason": "no_renderable_candidate"},
    ),
    PreviewRefusal.NO_GROUNDED_OUTFIT: (
        error_codes.STATE_CONFLICT,
        409,
        "This decision composed no outfit to render",
        {"reason": "no_grounded_outfit"},
    ),
}

_ERRORS: dict[int | str, dict[str, str]] = {
    404: {"description": "No such decision for this user (a foreign decision answers 404, not 403)."},
    409: {
        "description": (
            "`4091 STATE_CONFLICT`; `data.reason` is one of `avatar_not_ready`, "
            "`no_renderable_candidate`, `no_grounded_outfit`."
        )
    },
    429: {
        "description": (
            "`4091 STATE_CONFLICT`; `data.reason` is `daily_budget_exhausted` (the "
            "per-user daily try-on budget is spent, `ENABLE_COST_GUARD`) or "
            "`retry_budget_exhausted` (this generation has used its bounded retry "
            "allowance and will not be re-attempted). Both are retryable states for "
            "the client, on different horizons: tomorrow, and never for this preview."
        )
    },
    503: {"description": "`ENABLE_V3_STATIC_TRYON` is off."},
}


def _refuse(refusal: PreviewRefused) -> FriendlyException:
    code, status_code, message, data = _REFUSAL_RESPONSES[refusal.reason]
    return FriendlyException(code, message, status_code=status_code, data=data)


def _quality_retry_count(requested: PreviewRequested) -> int:
    # The analytics field is bounded 0..1 by the frozen contract ("at most one
    # model retry"); a deployment that raises TRYON_QUALITY_MAX_AUTO_REGENERATIONS
    # would otherwise produce an event the validator drops.
    return min(requested.quality_retry_count, 1)


def _emit_cache_hit_outcome(user_id: uuid.UUID, requested: PreviewRequested) -> None:
    if requested.created or requested.generation_status not in _TERMINAL_STATUSES:
        return
    flow_id = resolve_flow_id(structlog.contextvars.get_contextvars().get("correlation_id"))
    if flow_id is None:
        # `flow_id` is required on this event; a request outside any flow has
        # nothing to join and validation would (correctly) reject it.
        return
    emit_v3_event(
        V3AnalyticsEventName.PREVIEW_OUTCOME.value,
        {
            "generation_status": requested.generation_status,
            "cache_hit": True,
            "retry_count": _quality_retry_count(requested),
        },
        user_id=user_id,
        flow_id=flow_id,
    )


@router.post(
    "/{decision_id}/visual-preview",
    response_model=VisualPreviewResponse,
    responses=_ERRORS,
)
async def request_visual_preview(
    decision_id: uuid.UUID,
    payload: VisualPreviewRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    current_user: Annotated[User, Depends(get_current_user)],
    port: Annotated[StaticTryOnPort, Depends(get_static_tryon)],
) -> VisualPreviewResponse:
    # 200 on both branches (contract §6.1): a cache hit returns the existing
    # asset and a fresh request returns `queued` — a created-status would be
    # wrong whenever the cache answers, the V3-007 reasoning.
    correlation_id = structlog.contextvars.get_contextvars().get("correlation_id")
    try:
        requested = await RequestStaticPreview(
            session,
            settings=get_settings(),
            static_tryon=port,
        ).execute(
            user_id=current_user.id,
            decision_id=decision_id,
            correlation_id=str(correlation_id) if correlation_id else None,
            consent_confirmed=payload.consent,
        )
    except PreviewRefused as refusal:
        raise _refuse(refusal) from refusal
    _emit_cache_hit_outcome(current_user.id, requested)
    return VisualPreviewResponse(
        asset_id=requested.asset_id,
        generation_status=requested.generation_status,
    )
