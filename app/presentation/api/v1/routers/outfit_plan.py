"""Outfit Calendar router — V4. Off by default; V3-style direct return (no envelope).

Plans reference existing outfits (the system has no self-built outfits). A day can
hold multiple plans; status soft-cancels. Marking worn delegates to the existing
wear path. Calendar `worn` cells derive from WearHistory, `planned` from OutfitPlan.
"""

import uuid
from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.outfits.plan import (
    CancelPlan,
    CreateOutfitPlan,
    GetOutfitCalendar,
    MarkPlanWorn,
    OutfitPlanResult,
    ReschedulePlan,
)
from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.config.settings import get_settings
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.deps import get_current_user
from app.presentation.api.v1.schemas.outfit_history import OutfitSummary
from app.presentation.api.v1.schemas.outfit_plan import (
    CalendarCell,
    CalendarDayResponse,
    CreateOutfitPlanRequest,
    OutfitPlanCalendarResponse,
    OutfitPlanResponse,
    UpdateOutfitPlanRequest,
    WearPlanResponse,
)

router = APIRouter(prefix="/v1/outfit-plans", tags=["outfit-plan"])

_ALLOWED_STATUSES = {"planned", "cancelled"}


def _gate() -> None:
    if not get_settings().ENABLE_OUTFIT_CALENDAR:
        raise FriendlyException(
            error_codes.FORBIDDEN, "Outfit calendar is not enabled", status_code=503
        )


# /calendar registered before /{outfit_plan_id} to avoid path-param shadowing.
@router.get("/calendar", response_model=OutfitPlanCalendarResponse)
async def get_calendar(
    start: date,
    end: date,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> OutfitPlanCalendarResponse:
    _gate()
    repo = SqlAlchemyRecommendationRepository(session)
    days = await GetOutfitCalendar(repo).execute(current_user.id, start, end)
    return OutfitPlanCalendarResponse(
        days=[
            CalendarDayResponse(
                date=day.cell_date,
                cells=[
                    CalendarCell(
                        kind=cell.kind,
                        outfit=(
                            OutfitSummary(
                                outfit_id=cell.outfit.outfit_id,
                                outfit_type=cell.outfit.outfit_type,
                                title=cell.outfit.title,
                                reason_text=cell.outfit.reason_text,
                            )
                            if cell.outfit
                            else None
                        ),
                        wear_history_id=cell.wear_history_id,
                        ootd_image_url=cell.ootd_image_url,
                        outfit_plan_id=cell.outfit_plan_id,
                        status=cell.status,
                    )
                    for cell in day.cells
                ],
            )
            for day in days
        ]
    )


@router.post("", response_model=OutfitPlanResponse)
async def create_plan(
    payload: CreateOutfitPlanRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> OutfitPlanResponse:
    _gate()
    repo = SqlAlchemyRecommendationRepository(session)
    try:
        plan = await CreateOutfitPlan(repo).execute(
            current_user.id,
            payload.outfit_id,
            payload.planned_date,
        )
    except IntegrityError as exc:
        raise FriendlyException(
            error_codes.STATE_CONFLICT,
            "This outfit is already planned for that date",
            status_code=409,
        ) from exc
    return OutfitPlanResponse(
        outfit_plan_id=plan.id,
        outfit_id=plan.outfit_id,
        planned_date=plan.planned_date,
        status=plan.status,
    )


@router.patch("/{outfit_plan_id}", response_model=OutfitPlanResponse)
async def update_plan(
    outfit_plan_id: uuid.UUID,
    payload: UpdateOutfitPlanRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> OutfitPlanResponse:
    _gate()
    if payload.status is not None and payload.status not in _ALLOWED_STATUSES:
        raise FriendlyException(
            error_codes.VALIDATION_ERROR,
            "status must be one of: planned, cancelled",
            status_code=422,
        )
    repo = SqlAlchemyRecommendationRepository(session)
    user_id = current_user.id
    existing = await repo.get_outfit_plan_for_user(outfit_plan_id, user_id)
    if existing is None:
        raise FriendlyException(error_codes.NOT_FOUND, "Plan not found", status_code=404)
    # Reschedule (planned_date) and cancel (status) may arrive together — apply both.
    # Keep result as OutfitPlanResult throughout to avoid ORM/dataclass type mix.
    result: OutfitPlanResult | None = None
    try:
        if payload.planned_date is not None:
            result = await ReschedulePlan(repo).execute(
                outfit_plan_id, user_id, payload.planned_date
            )
        if payload.status == "cancelled":
            result = await CancelPlan(repo).execute(outfit_plan_id, user_id)
        elif payload.status == "planned":
            existing.status = "planned"
            orm_plan = await repo.update_outfit_plan(existing)
            result = OutfitPlanResult.from_model(orm_plan)
    except IntegrityError as exc:
        raise FriendlyException(
            error_codes.STATE_CONFLICT,
            "This outfit is already planned for that date",
            status_code=409,
        ) from exc
    # If neither field was provided, reflect current state unchanged.
    final = result if result is not None else OutfitPlanResult.from_model(existing)
    return OutfitPlanResponse(
        outfit_plan_id=final.id,
        outfit_id=final.outfit_id,
        planned_date=final.planned_date,
        status=final.status,
    )


@router.post("/{outfit_plan_id}/wear", response_model=WearPlanResponse)
async def mark_worn(
    outfit_plan_id: uuid.UUID,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    current_user: Annotated[User, Depends(get_current_user)],
) -> WearPlanResponse:
    _gate()
    repo = SqlAlchemyRecommendationRepository(session)
    wear_result = await MarkPlanWorn(repo).execute(outfit_plan_id, current_user.id)
    return WearPlanResponse(
        outfit_plan_id=outfit_plan_id,
        wear_history_id=wear_result.wear_history_id,
        worn_date=wear_result.worn_date,
        status="cancelled",
    )
