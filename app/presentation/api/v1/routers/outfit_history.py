"""Outfit history & OOTD router — M11."""

import uuid
from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.infrastructure.persistence.models.recommendation import Outfit, OutfitFeedback, WearHistory
from app.infrastructure.persistence.session import get_db_session as get_db
from app.presentation.api.v1.deps import get_current_user
from app.presentation.api.v1.schemas.outfit_history import (
    OotdUploadRequest,
    OotdUploadResponse,
    OutfitCalendarResponse,
    OutfitHistoryEntry,
    OutfitHistoryListResponse,
    OutfitSummary,
)

router = APIRouter(prefix="/v1/outfit-history", tags=["outfit-history"])


@router.get("", response_model=OutfitHistoryListResponse)
async def list_outfit_history(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[Any, Depends(get_current_user)],
    page: Annotated[int, Query(ge=1)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
) -> OutfitHistoryListResponse:
    offset = (page - 1) * page_size

    count_result = await db.execute(
        select(func.count()).select_from(WearHistory).where(WearHistory.user_id == current_user.id)
    )
    total = count_result.scalar_one()

    result = await db.execute(
        select(WearHistory)
        .where(WearHistory.user_id == current_user.id)
        .order_by(WearHistory.worn_date.desc())
        .offset(offset)
        .limit(page_size)
    )
    histories = list(result.scalars().all())

    if not histories:
        return OutfitHistoryListResponse(items=[], page=page, page_size=page_size, total=total)

    # Batch-load outfits and feedbacks to avoid N+1
    outfit_ids = [h.outfit_id for h in histories]

    outfit_result = await db.execute(select(Outfit).where(Outfit.id.in_(outfit_ids)))
    outfits_by_id = {o.id: o for o in outfit_result.scalars().all()}

    fb_result = await db.execute(
        select(OutfitFeedback).where(
            OutfitFeedback.outfit_id.in_(outfit_ids),
            OutfitFeedback.user_id == current_user.id,
        )
    )
    feedbacks_by_outfit = {fb.outfit_id: fb for fb in fb_result.scalars().all()}

    entries = []
    for h in histories:
        outfit = outfits_by_id.get(h.outfit_id)
        outfit_summary = None
        if outfit:
            outfit_summary = OutfitSummary(
                outfit_id=outfit.id,
                outfit_type=outfit.outfit_type,
                title=outfit.title,
                reason_text=outfit.reason_text,
            )

        fb = feedbacks_by_outfit.get(h.outfit_id)
        entries.append(
            OutfitHistoryEntry(
                wear_history_id=h.id,
                worn_date=h.worn_date,
                outfit=outfit_summary,
                feedback_type=fb.feedback_type if fb else None,
                reason_tags=list(fb.reason_tags or []) if fb else [],
                ootd_image_url=h.ootd_image_url,
                selected_at=h.selected_at,
            )
        )

    return OutfitHistoryListResponse(items=entries, page=page, page_size=page_size, total=total)


# /calendar must be registered before /{worn_date} to avoid path parameter shadowing
@router.get("/calendar", response_model=OutfitCalendarResponse)
async def get_outfit_calendar(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[Any, Depends(get_current_user)],
    year: Annotated[int | None, Query()] = None,
    month: Annotated[int | None, Query()] = None,
) -> OutfitCalendarResponse:
    stmt = select(WearHistory.worn_date).where(WearHistory.user_id == current_user.id)
    if year is not None:
        stmt = stmt.where(func.extract("year", WearHistory.worn_date) == year)
    if month is not None:
        stmt = stmt.where(func.extract("month", WearHistory.worn_date) == month)
    stmt = stmt.distinct().order_by(WearHistory.worn_date)

    result = await db.execute(stmt)
    dates = [row[0] for row in result.all()]
    return OutfitCalendarResponse(dates=dates)


@router.get("/{worn_date}", response_model=OutfitHistoryEntry)
async def get_outfit_history_by_date(
    worn_date: date,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[Any, Depends(get_current_user)],
) -> OutfitHistoryEntry:
    result = await db.execute(
        select(WearHistory).where(
            WearHistory.user_id == current_user.id,
            WearHistory.worn_date == worn_date,
        )
    )
    h = result.scalar_one_or_none()
    if h is None:
        raise FriendlyException(error_codes.NOT_FOUND, "No record for that date", status_code=404)

    outfit_summary = None
    outfit_result = await db.execute(select(Outfit).where(Outfit.id == h.outfit_id))
    outfit = outfit_result.scalar_one_or_none()
    if outfit:
        outfit_summary = OutfitSummary(
            outfit_id=outfit.id,
            outfit_type=outfit.outfit_type,
            title=outfit.title,
            reason_text=outfit.reason_text,
        )

    return OutfitHistoryEntry(
        wear_history_id=h.id,
        worn_date=h.worn_date,
        outfit=outfit_summary,
        ootd_image_url=h.ootd_image_url,
        selected_at=h.selected_at,
    )


@router.post("/{wear_history_id}/ootd", response_model=OotdUploadResponse)
async def upload_ootd(
    wear_history_id: uuid.UUID,
    body: OotdUploadRequest,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[Any, Depends(get_current_user)],
) -> OotdUploadResponse:
    result = await db.execute(
        select(WearHistory).where(
            WearHistory.id == wear_history_id,
            WearHistory.user_id == current_user.id,
        )
    )
    h = result.scalar_one_or_none()
    if h is None:
        raise FriendlyException(error_codes.NOT_FOUND, "Record not found", status_code=404)

    h.ootd_image_url = body.ootd_image_url
    await db.flush()
    return OotdUploadResponse(wear_history_id=h.id, ootd_image_url=body.ootd_image_url)
