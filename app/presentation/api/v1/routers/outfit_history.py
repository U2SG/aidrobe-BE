"""Outfit history & OOTD router — M11."""

import uuid
from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.persistence.models.recommendation import Outfit, OutfitFeedback, WearHistory
from app.presentation.api.v1.deps import get_current_user
from app.infrastructure.persistence.session import get_db_session as get_db
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
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
) -> OutfitHistoryListResponse:
    offset = (page - 1) * page_size

    count_result = await db.execute(
        select(func.count()).where(WearHistory.user_id == current_user.id)
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

    entries = []
    for h in histories:
        outfit_summary = None
        feedback_type = None
        reason_tags: list[str] = []

        outfit_result = await db.execute(select(Outfit).where(Outfit.id == h.outfit_id))
        outfit = outfit_result.scalar_one_or_none()
        if outfit:
            outfit_summary = OutfitSummary(
                outfit_id=outfit.id,
                outfit_type=outfit.outfit_type,
                title=outfit.title,
                reason_text=outfit.reason_text,
            )

        fb_result = await db.execute(
            select(OutfitFeedback).where(
                OutfitFeedback.outfit_id == h.outfit_id,
                OutfitFeedback.user_id == current_user.id,
            )
        )
        fb = fb_result.scalar_one_or_none()
        if fb:
            feedback_type = fb.feedback_type
            reason_tags = list(fb.reason_tags or [])

        entries.append(
            OutfitHistoryEntry(
                wear_history_id=h.id,
                worn_date=h.worn_date,
                outfit=outfit_summary,
                feedback_type=feedback_type,
                reason_tags=reason_tags,
                ootd_image_url=getattr(h, "ootd_image_url", None),
                selected_at=h.selected_at,
            )
        )

    return OutfitHistoryListResponse(items=entries, page=page, page_size=page_size, total=total)


@router.get("/calendar", response_model=OutfitCalendarResponse)
async def get_outfit_calendar(
    year: int | None = Query(None),
    month: int | None = Query(None),
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
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
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
) -> OutfitHistoryEntry:
    result = await db.execute(
        select(WearHistory).where(
            WearHistory.user_id == current_user.id,
            WearHistory.worn_date == worn_date,
        )
    )
    h = result.scalar_one_or_none()
    if h is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No record for that date")

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
        ootd_image_url=getattr(h, "ootd_image_url", None),
        selected_at=h.selected_at,
    )


@router.post("/{wear_history_id}/ootd", response_model=OotdUploadResponse)
async def upload_ootd(
    wear_history_id: uuid.UUID,
    body: OotdUploadRequest,
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
) -> OotdUploadResponse:
    result = await db.execute(
        select(WearHistory).where(
            WearHistory.id == wear_history_id,
            WearHistory.user_id == current_user.id,
        )
    )
    h = result.scalar_one_or_none()
    if h is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Record not found")

    h.ootd_image_url = body.ootd_image_url  # type: ignore[attr-defined]
    await db.flush()
    return OotdUploadResponse(wear_history_id=h.id, ootd_image_url=body.ootd_image_url)
