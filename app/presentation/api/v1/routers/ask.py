"""Ask Aidrobe router — M15.

V3-style direct returns (no `{code,message,data}` envelope), matching the
insights / outfit-visual surfaces. Exposure is gated behind
`ENABLE_ASK_AIDROBE`; when off the endpoints return 503. Data persists
regardless of the flag so flipping it back is a clean rollback.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ask import answer_question as ask_uc
from app.application.ports.llm import LlmPort
from app.bootstrap.container import llm_service
from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.config.settings import get_settings
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.deps import get_current_user
from app.presentation.api.v1.schemas.ask import (
    AskHistoryItem,
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
    )
    return AskResponse(
        ask_session_id=result.ask_session_id,
        answer=result.answer,
        item_refs=result.item_refs,
        tokens_used=result.tokens_used,
    )


@router.get("/history", response_model=list[AskHistoryItem])
async def ask_history(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    limit: int = Query(20, ge=1, le=100),
    offset: int = Query(0, ge=0),
) -> list[AskHistoryItem]:
    _gate()
    rows = await ask_uc.list_history(
        session, current_user.id, limit=limit, offset=offset
    )
    return [
        AskHistoryItem(
            ask_session_id=row.id,
            question=row.question,
            answer=row.answer,
            item_refs=list(row.item_refs or []),
            created_at=row.created_at,
        )
        for row in rows
    ]
