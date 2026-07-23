"""Ask Aidrobe router — M15.

V3-style direct returns (no `{code,message,data}` envelope), matching the
insights / outfit-visual surfaces. Exposure is gated behind
`ENABLE_ASK_AIDROBE`; when off the endpoints return 503. Data persists
regardless of the flag so flipping it back is a clean rollback.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ask import answer_question as ask_uc
from app.application.ask.answer_question import ItemRefSnapshot, build_item_ref_snapshots
from app.application.ports.llm import LlmPort
from app.bootstrap.container import llm_service
from app.common import error_codes
from app.common.enums import FeedbackType, RecommendationEventType
from app.common.exceptions import FriendlyException
from app.config.settings import get_settings
from app.infrastructure.persistence.models.ask_session import AskSession
from app.infrastructure.persistence.models.recommendation import RecommendationEvent
from app.infrastructure.persistence.models.user import User, UserSettings
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.deps import get_current_user
from app.presentation.api.v1.render_url import resolve_render_url
from app.presentation.api.v1.schemas.ask import (
    AskConversationDeltas,
    AskHistoryItem,
    AskItemRefSnapshot,
    AskRequest,
    AskResponse,
    AskTripIntent,
)
from app.presentation.api.v1.schemas.recommendations import FeedbackRequest, FeedbackResponse

# BE#86 — mirrors FeedbackOutfit._FEEDBACK_EVENT_MAP; kept local so the ask
# router has no import dependency on the outfit feedback use case.
_ASK_FEEDBACK_EVENT_MAP = {
    FeedbackType.LOVE.value: RecommendationEventType.LIKED.value,
    FeedbackType.LIKE.value: RecommendationEventType.LIKED.value,
    FeedbackType.NEUTRAL.value: RecommendationEventType.NEUTRAL.value,
    FeedbackType.DISLIKE.value: RecommendationEventType.DISLIKED.value,
}

router = APIRouter(prefix="/v1/ask", tags=["ask"])


def _gate() -> None:
    if not get_settings().ENABLE_ASK_AIDROBE:
        raise FriendlyException(
            error_codes.INTERNAL_ERROR,
            "Ask Aidrobe is not enabled",
            status_code=503,
        )


def _to_schema(snapshots: list[ItemRefSnapshot]) -> list[AskItemRefSnapshot]:
    """Sign each snapshot's thumbnail at request time (never persist a
    TTL-bound URL) and project to the response schema."""
    return [
        AskItemRefSnapshot(
            item_id=snap.item_id,
            name=snap.name,
            category=snap.category,
            color_main=snap.color_main,
            thumbnail_url=resolve_render_url(snap.thumbnail_object_key, snap.thumbnail_fallback_url),
        )
        for snap in snapshots
    ]


@router.post("", response_model=AskResponse)
async def ask(
    payload: AskRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    llm: Annotated[LlmPort, Depends(llm_service)],
) -> AskResponse:
    _gate()
    context = payload.context.model_dump(exclude_none=True) if payload.context else None
    result = await ask_uc.answer_question(
        session,
        current_user.id,
        question=payload.question,
        context=context,
        llm=llm,
        locale=payload.locale,
        focused_item_id=payload.focused_item_id,
        conversation_id=payload.conversation_id,
    )
    # Reuse the locale answer_question already resolved (request > settings >
    # default) — no second settings query. Snapshot names use the request
    # locale here; the history endpoint (no request locale) uses settings/default.
    snapshots = await build_item_ref_snapshots(
        session,
        current_user.id,
        result.item_refs,
        locale=result.effective_locale,
    )
    expose_conversation = get_settings().ENABLE_ASK_CONVERSATIONS
    # BE#86 — recommendation_id is set to ask_session_id when the answer
    # references wardrobe items, giving the client a stable anchor for feedback.
    # Pure-text answers (no cited items) leave it None.
    recommendation_id = result.ask_session_id if result.item_refs else None
    return AskResponse(
        ask_session_id=result.ask_session_id,
        conversation_id=result.conversation_id if expose_conversation else None,
        answer=result.answer,
        item_refs=result.item_refs,
        item_ref_snapshots=_to_schema(snapshots),
        has_wardrobe_context=result.has_wardrobe_context,
        locale=result.effective_locale,
        tokens_used=result.tokens_used,
        deltas=(
            AskConversationDeltas(
                reasoning=result.deltas.reasoning,
                applied_constraints=result.deltas.applied_constraints,
            )
            if expose_conversation
            else None
        ),
        recommendation_id=recommendation_id,
        trip_intent=(
            AskTripIntent(
                is_trip=result.trip_intent.is_trip,
                duration_days=result.trip_intent.duration_days,
            )
            if result.trip_intent is not None
            else None
        ),
    )


@router.get("/history", response_model=list[AskHistoryItem])
async def ask_history(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
    # BE#29 — scope the history to one item. When set, only Ask sessions
    # related to that item are returned (context snapshot focus or stored
    # item_refs); old context-less rows stay in the global view only.
    focused_item_id: Annotated[uuid.UUID | None, Query()] = None,
    # Reserved for the scoped-Ask UI / FE telemetry. Declared so they appear in
    # the OpenAPI contract the FE generates against; the backend does not filter
    # on them yet (undeclared query params would be silently ignored either way).
    scope: Annotated[str | None, Query(max_length=32)] = None,
    entrypoint: Annotated[str | None, Query(max_length=64)] = None,
) -> list[AskHistoryItem]:
    _gate()
    rows = await ask_uc.list_history(
        session,
        current_user.id,
        limit=limit,
        offset=offset,
        focused_item_id=focused_item_id,
    )
    # Batch-fetch every referenced item across the page in one query, then map
    # signed snapshots back per row — no per-row, no per-id N+1.
    all_refs: list[str] = []
    for row in rows:
        all_refs.extend(str(ref) for ref in (row.item_refs or []))
    settings_locale = await session.scalar(
        select(UserSettings.locale).where(UserSettings.user_id == current_user.id)
    )
    snapshots = await build_item_ref_snapshots(
        session, current_user.id, all_refs, settings_locale=settings_locale
    )
    by_id = {str(snap.item_id): snap for snap in snapshots}
    out: list[AskHistoryItem] = []
    for row in rows:
        refs = [str(ref) for ref in (row.item_refs or [])]
        row_snaps = [by_id[ref] for ref in refs if ref in by_id]
        out.append(
            AskHistoryItem(
                ask_session_id=row.id,
                question=row.question,
                answer=row.answer,
                item_refs=refs,
                item_ref_snapshots=_to_schema(row_snaps),
                created_at=row.created_at,
            )
        )
    return out


@router.post("/{ask_session_id}/feedback", response_model=FeedbackResponse)
async def ask_feedback(
    ask_session_id: uuid.UUID,
    payload: FeedbackRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> FeedbackResponse:
    """BE#86 — record a feedback signal against an Ask session.

    Loads the AskSession and enforces ownership (404 on missing or foreign —
    never reveals existence). Writes a RecommendationEvent with source
    "ask_feedback" so the event stream accumulates per-session signals.
    Idempotent: repeated calls append new events (same as Today feedback).
    """
    _gate()
    ask_session = await session.get(AskSession, ask_session_id)
    if ask_session is None or ask_session.user_id != current_user.id:
        raise FriendlyException(error_codes.NOT_FOUND, "Ask session not found", status_code=404)

    event_type = _ASK_FEEDBACK_EVENT_MAP.get(payload.feedback_type)
    if event_type is None:
        raise FriendlyException(
            error_codes.VALIDATION_ERROR, "Invalid feedback type", status_code=422
        )

    # Extract focused_item_id and conversation_id from the JSONB context blob.
    ctx = ask_session.context or {}
    snapshot = ctx.get("snapshot") or {}
    conversation = ctx.get("conversation") or {}
    focused_item_id: str | None = snapshot.get("focused_item_id")
    conversation_id: str | None = conversation.get("conversation_id")
    item_refs: list[str] = [str(r) for r in (ask_session.item_refs or [])]

    repo = SqlAlchemyRecommendationRepository(session)
    await repo.add_recommendation_event(
        RecommendationEvent(
            user_id=current_user.id,
            recommendation_id=None,  # ask_session is not a daily_recommendation row
            outfit_id=None,
            event_type=event_type,
            source="ask_feedback",
            event_metadata={
                "ask_session_id": str(ask_session_id),
                "source_surface": "ask",
                # `item_ids` is the key derive_learning_signals()/_signature()
                # reads to build a suppression signature — Today feedback uses
                # the same key. Use it (not a bespoke "item_refs") so Ask dislikes
                # actually feed feedback-learning instead of being dead records.
                "item_ids": item_refs,
                "focused_item_id": focused_item_id,
                "reason_tags": payload.reason_tags,
                "conversation_id": conversation_id,
            },
            created_at=datetime.now(UTC),
        )
    )
    return FeedbackResponse(success=True)
