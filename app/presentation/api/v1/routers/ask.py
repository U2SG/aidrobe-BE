"""Ask Aidrobe router — M15.

V3-style direct returns (no `{code,message,data}` envelope), matching the
insights / outfit-visual surfaces. Exposure is gated behind
`ENABLE_ASK_AIDROBE`; when off the endpoints return 503. Data persists
regardless of the flag so flipping it back is a clean rollback.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ask import answer_question as ask_uc
from app.application.ask.answer_question import ItemRefSnapshot, build_item_ref_snapshots
from app.application.ports.llm import LlmPort
from app.bootstrap.container import llm_service
from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.config.settings import get_settings
from app.infrastructure.persistence.models.user import User, UserSettings
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.deps import get_current_user
from app.presentation.api.v1.render_url import resolve_render_url
from app.presentation.api.v1.schemas.ask import (
    AskConversationDeltas,
    AskHistoryItem,
    AskItemRefSnapshot,
    AskRequest,
    AskResponse,
)

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
