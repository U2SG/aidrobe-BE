"""Explicit design experiences. Direct JSON, following the Ask contract."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.outfits.experiences import StylingExperienceService
from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.config.settings import get_settings
from app.infrastructure.persistence.models.styling_experience import StylingExperience
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.deps import get_current_user
from app.presentation.api.v1.schemas.styling_experiences import (
    StylingExperienceDeleteResponse,
    StylingExperienceListResponse,
    StylingExperienceRequest,
    StylingExperienceResponse,
)

router = APIRouter(prefix="/v1/styling-experiences", tags=["styling-experiences"])


def _gate() -> None:
    if not get_settings().ENABLE_DESIGNER_SESSIONS:
        raise FriendlyException(error_codes.FORBIDDEN, "Designer sessions are not enabled", 503)


def _render(row: StylingExperience) -> StylingExperienceResponse:
    return StylingExperienceResponse.model_validate({
        "experience_id": row.id, "request_id": row.request_id, "outfit_id": row.outfit_id,
        "kind": row.kind, "conversation_id": row.conversation_id, "revision": row.revision,
        "worn_date": row.worn_date, "feedback": row.feedback, "note": row.note,
        "occasion": row.occasion, "supersedes_id": row.supersedes_id, "created_at": row.created_at,
    })


@router.post("", response_model=StylingExperienceResponse)
async def record_experience(
    payload: StylingExperienceRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> StylingExperienceResponse:
    _gate()
    return _render(await StylingExperienceService(session, current_user.id).record(payload))


@router.get("", response_model=StylingExperienceListResponse)
async def list_experiences(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    outfit_id: uuid.UUID | None = None,
    limit: int = Query(20, ge=3, le=100),
) -> StylingExperienceListResponse:
    _gate()
    service = StylingExperienceService(session, current_user.id)
    if outfit_id is not None and await service.recommendations.get_outfit_for_user(outfit_id, current_user.id) is None:
        raise FriendlyException(error_codes.NOT_FOUND, "Outfit not found", 404)
    rows = await service.repository.list_live(current_user.id, outfit_id=outfit_id, limit=limit)
    latest_worn_date = (
        await service.latest_worn_date(outfit_id) if outfit_id is not None else None
    )
    return StylingExperienceListResponse(items=[_render(row) for row in rows], latest_worn_date=latest_worn_date)


@router.delete("/{experience_id}", response_model=StylingExperienceDeleteResponse)
async def withdraw_experience(
    experience_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> StylingExperienceDeleteResponse:
    _gate()
    await StylingExperienceService(session, current_user.id).withdraw(experience_id)
    return StylingExperienceDeleteResponse()
