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
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ask import answer_question as ask_uc
from app.application.ask.answer_question import ItemRefSnapshot, build_item_ref_snapshots
from app.application.ask.design_types import DesignBrief
from app.application.ask.styling import StylingService
from app.application.packing.generate import load_conversation_packing_list
from app.application.ports.llm import LlmPort
from app.application.ports.weather import WeatherPort
from app.bootstrap.container import llm_service, weather_service
from app.common import error_codes
from app.common.enums import FeedbackType, RecommendationEventType
from app.common.exceptions import FriendlyException
from app.common.local_time import local_datetime, utc_now
from app.config.settings import get_settings
from app.infrastructure.cache.client import get_redis_client, invalidate_daily_recommendation_cache
from app.infrastructure.persistence.models.ask_conversation import AskMessage
from app.infrastructure.persistence.models.ask_session import AskSession
from app.infrastructure.persistence.models.recommendation import RecommendationEvent
from app.infrastructure.persistence.models.user import User, UserSettings
from app.infrastructure.persistence.repositories.ask_conversation_repo import SqlAlchemyAskConversationRepository
from app.infrastructure.persistence.repositories.import_task_repo import SqlAlchemyImportTaskRepository
from app.infrastructure.persistence.repositories.purchase_decision_repo import SqlAlchemyWardrobeDecisionRepository
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)
from app.infrastructure.persistence.repositories.user_repo import SqlAlchemyUserRepository
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.deps import get_current_user
from app.presentation.api.v1.render_url import resolve_render_url
from app.presentation.api.v1.routers.import_tasks import render_import_task
from app.presentation.api.v1.routers.packing_list import render_packing_list
from app.presentation.api.v1.routers.purchase_candidates import render_purchase_decisions
from app.presentation.api.v1.routers.recommendations import render_styling
from app.presentation.api.v1.schemas.ask import (
    AdoptStylingRequest,
    AskConversationDeltas,
    AskConversationMessage,
    AskConversationResponse,
    AskHistoryItem,
    AskItemRefSnapshot,
    AskRequest,
    AskResponse,
    AskToolTrace,
    AskToolUse,
    AskTripIntent,
    CreateOutfitRevisionRequest,
    StylingResponse,
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


def _ask_weather_service() -> WeatherPort | None:
    return weather_service() if get_settings().ENABLE_ASK_TOOLS else None


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
    weather: Annotated[WeatherPort | None, Depends(_ask_weather_service)],
) -> AskResponse:
    _gate()
    context = payload.context.model_dump(exclude_none=True) if payload.context else None
    result = await ask_uc.answer_question(
        session,
        current_user.id,
        question=payload.question,
        context=context,
        llm=llm,
        weather=weather,
        locale=payload.locale,
        focused_item_id=payload.focused_item_id,
        conversation_id=payload.conversation_id,
        design_mode=payload.design_mode,
        request_id=payload.request_id,
        expected_revision=payload.expected_revision,
        base_outfit_id=payload.base_outfit_id,
        inspiration_id=payload.inspiration_id,
        inspiration_only=payload.inspiration_only,
        constraints=payload.constraints,
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
    expose_conversation = get_settings().ENABLE_ASK_CONVERSATIONS or get_settings().ENABLE_DESIGNER_SESSIONS
    # BE#86 — recommendation_id is set to ask_session_id when the answer
    # references wardrobe items, giving the client a stable anchor for feedback.
    # Pure-text answers (no cited items) leave it None.
    recommendation_id = result.ask_session_id if result.item_refs else None
    return AskResponse(
        ask_session_id=result.ask_session_id,
        styling=await render_styling(session, current_user.id, result.styling),
        design_brief=result.design_brief,
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
        used=[AskToolUse(tool=u.tool, summary=u.summary) for u in result.used],
        tool_trace=[AskToolTrace(
            tool=entry.tool, arguments=entry.arguments, result=entry.result,
            status=entry.status, duration_ms=entry.duration_ms,
        ) for entry in result.tool_trace],
        failure_reason=result.failure_reason,
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
    # Resolve each session's conversation through ask_messages (sessions carry
    # no conversation column; messages do). One IN-query for the page — the FE
    # uses this to resume a past conversation (#114). Exposure follows
    # ENABLE_ASK_CONVERSATIONS, same as POST /v1/ask — rows persist regardless.
    conversation_by_session: dict[uuid.UUID, uuid.UUID] = {}
    if rows and (get_settings().ENABLE_ASK_CONVERSATIONS or get_settings().ENABLE_DESIGNER_SESSIONS):
        message_rows = await session.execute(
            select(AskMessage.ask_session_id, AskMessage.conversation_id).where(
                AskMessage.ask_session_id.in_([row.id for row in rows])
            )
        )
        for ask_session_id, conversation_id in message_rows:
            if ask_session_id is not None:
                conversation_by_session[ask_session_id] = conversation_id
    out: list[AskHistoryItem] = []
    for row in rows:
        refs = [str(ref) for ref in (row.item_refs or [])]
        row_snaps = [by_id[ref] for ref in refs if ref in by_id]
        out.append(
            AskHistoryItem(
                ask_session_id=row.id,
                conversation_id=conversation_by_session.get(row.id),
                question=row.question,
                answer=row.answer,
                item_refs=refs,
                item_ref_snapshots=_to_schema(row_snaps),
                created_at=row.created_at,
                trip_intent=_trip_intent_from_context(row.context),
                styling=await _styling_from_metadata(session, current_user.id, row.context),
                design_brief=_brief_from_metadata(row.context),
            )
        )
    return out


async def _styling_from_metadata(
    session: AsyncSession, user_id: uuid.UUID, metadata: dict[str, object] | None,
) -> StylingResponse | None:
    if not get_settings().ENABLE_DESIGNER_SESSIONS:
        return None
    raw = (metadata or {}).get("styling_revision_id")
    if not isinstance(raw, str):
        return None
    try:
        revision_id = uuid.UUID(raw)
    except ValueError:
        return None
    revision = await StylingService(session, user_id).get_revision(revision_id)
    return await render_styling(session, user_id, revision)


def _brief_from_metadata(metadata: dict[str, object] | None) -> DesignBrief | None:
    raw = (metadata or {}).get("design_brief")
    if not get_settings().ENABLE_DESIGNER_SESSIONS or not isinstance(raw, dict):
        return None
    try:
        return DesignBrief.model_validate(raw)
    except ValueError:
        return None


def _design_gate() -> None:
    _gate()
    if not get_settings().ENABLE_DESIGNER_SESSIONS:
        raise FriendlyException(error_codes.FORBIDDEN, "Designer sessions are not enabled", 503)


@router.get("/conversations/{conversation_id}", response_model=AskConversationResponse)
async def get_conversation(
    conversation_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> AskConversationResponse:
    _gate()
    repository = SqlAlchemyAskConversationRepository(session)
    conversation = await repository.get_owned_conversation(current_user.id, conversation_id)
    if conversation is None:
        raise FriendlyException(error_codes.NOT_FOUND, "Conversation not found", 404)
    messages = await repository.list_messages(current_user.id, conversation_id)
    service = StylingService(session, current_user.id)
    current = await service.current(conversation_id)
    session_ids = {message.ask_session_id for message in messages if message.ask_session_id is not None}
    turns = list(await session.scalars(select(AskSession).where(
        AskSession.id.in_(session_ids), AskSession.user_id == current_user.id,
    ))) if session_ids else []
    by_session = {turn.id: turn for turn in turns}
    settings_locale = await session.scalar(select(UserSettings.locale).where(UserSettings.user_id == current_user.id))
    snapshots = await build_item_ref_snapshots(
        session, current_user.id, [str(ref) for turn in turns for ref in (turn.item_refs or [])],
        settings_locale=settings_locale,
    )
    by_item = {str(snap.item_id): snap for snap in snapshots}
    restored: list[AskConversationMessage] = []
    for message in messages:
        turn = by_session.get(message.ask_session_id) if (
            message.role == "assistant" and message.ask_session_id is not None
        ) else None
        refs = [str(ref) for ref in (turn.item_refs or [])] if turn is not None else []
        restored.append(AskConversationMessage(
            id=message.id, ask_session_id=message.ask_session_id, role=message.role,
            content=message.content, created_at=message.created_at,
            styling=await _styling_from_metadata(session, current_user.id, message.message_metadata),
            design_brief=_brief_from_metadata(message.message_metadata),
            item_refs=refs, item_ref_snapshots=_to_schema([by_item[ref] for ref in refs if ref in by_item]),
            trip_intent=_trip_intent_from_context(turn.context) if turn is not None else None,
        ))
    settings = get_settings()
    packing_list = None
    if settings.ENABLE_DESIGNER_SESSIONS and settings.ENABLE_PACKING_LIST:
        packing_list = await load_conversation_packing_list(session, current_user.id, conversation.id)
    purchases = []
    if settings.ENABLE_DESIGNER_SESSIONS and settings.ENABLE_V3_PURCHASE_DECISION:
        purchases = await SqlAlchemyWardrobeDecisionRepository(session).list_for_conversation(
            current_user.id, conversation.id,
        )
    return AskConversationResponse(
        conversation_id=conversation.id, title=conversation.title,
        styling=(
            await render_styling(session, current_user.id, current) if settings.ENABLE_DESIGNER_SESSIONS else None
        ),
        design_brief=service.brief(conversation, current) if settings.ENABLE_DESIGNER_SESSIONS else None,
        packing_list=render_packing_list(packing_list) if packing_list is not None else None,
        purchase_decisions=await render_purchase_decisions(session, purchases),
        import_tasks=[
            await render_import_task(session, task)
            for task in await SqlAlchemyImportTaskRepository(session).list_for_conversation(
                current_user.id, conversation.id,
            )
        ],
        messages=restored,
    )


@router.post("/conversations/{conversation_id}/outfit-revisions", response_model=StylingResponse)
async def create_outfit_revision(
    conversation_id: uuid.UUID,
    payload: CreateOutfitRevisionRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> StylingResponse:
    _design_gate()
    if payload.replace is None and payload.locked_item_ids is None and payload.excluded_item_ids is None:
        raise FriendlyException(error_codes.VALIDATION_ERROR, "Provide a replacement or updated constraints", 422)
    revision = await StylingService(session, current_user.id).revise(
        conversation_id=conversation_id, expected_revision=payload.expected_revision, request_id=payload.request_id,
        replace=(payload.replace.item_id, payload.replace.replacement_item_id) if payload.replace else None,
        locked_item_ids=payload.locked_item_ids, excluded_item_ids=payload.excluded_item_ids,
    )
    result = await render_styling(session, current_user.id, revision)
    assert result is not None
    return result


@router.post("/conversations/{conversation_id}/adopt", response_model=StylingResponse)
async def adopt_styling(
    conversation_id: uuid.UUID,
    payload: AdoptStylingRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    redis_client: Annotated[Redis, Depends(get_redis_client)],
) -> StylingResponse:
    _design_gate()
    user_settings = await SqlAlchemyUserRepository(session).get_or_create_settings(current_user.id)
    local_today = local_datetime(utc_now(), user_settings.timezone, get_settings().DEFAULT_TIMEZONE).date()
    revision = await StylingService(session, current_user.id).adopt(
        conversation_id=conversation_id, expected_revision=payload.expected_revision, request_id=payload.request_id,
        day=payload.date, default_day=local_today,
    )
    await invalidate_daily_recommendation_cache(redis_client, current_user.id)
    result = await render_styling(session, current_user.id, revision)
    assert result is not None
    return result


def _trip_intent_from_context(context: dict[str, object] | None) -> AskTripIntent | None:
    """BE#120 — re-materialize the persisted trip signal (answer_question only
    stores affirmative intents). Malformed/legacy rows collapse to None."""
    raw = (context or {}).get("trip_intent")
    if not isinstance(raw, dict) or raw.get("is_trip") is not True:
        return None
    duration = raw.get("duration_days")
    if isinstance(duration, bool) or not isinstance(duration, int):
        duration = None
    return AskTripIntent(is_trip=True, duration_days=duration)


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
