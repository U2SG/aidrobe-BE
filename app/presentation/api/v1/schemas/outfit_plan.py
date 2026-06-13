"""Outfit plan / calendar schemas — V4. V3-style direct return (no envelope)."""

import uuid
from datetime import date

from pydantic import BaseModel

from app.presentation.api.v1.schemas.outfit_history import OutfitSummary


class CreateOutfitPlanRequest(BaseModel):
    outfit_id: uuid.UUID
    planned_date: date


class UpdateOutfitPlanRequest(BaseModel):
    planned_date: date | None = None
    status: str | None = None


class OutfitPlanResponse(BaseModel):
    outfit_plan_id: uuid.UUID
    outfit_id: uuid.UUID
    planned_date: date
    status: str


class WearPlanResponse(BaseModel):
    outfit_plan_id: uuid.UUID
    wear_history_id: uuid.UUID
    worn_date: date
    status: str


class CalendarCell(BaseModel):
    kind: str  # "worn" | "planned"
    outfit: OutfitSummary | None = None
    # worn
    wear_history_id: uuid.UUID | None = None
    ootd_image_url: str | None = None
    # planned
    outfit_plan_id: uuid.UUID | None = None
    status: str | None = None


class CalendarDayResponse(BaseModel):
    date: date
    cells: list[CalendarCell]


class OutfitCalendarResponse(BaseModel):
    days: list[CalendarDayResponse]
