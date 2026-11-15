"""Packing list (M16) schemas — V3-style direct return (no envelope)."""

import uuid
from datetime import date
from typing import Annotated, Self

from pydantic import BaseModel, Field, model_validator

from app.application.ask.continuity_types import DesignContextRef, DesignContextSnapshot


class PackingItem(BaseModel):
    item_id: uuid.UUID
    day: int
    scene: str
    notes: str = ""
    packed: bool = False


class CreatePackingListRequest(BaseModel):
    destination: str | None = None
    days: Annotated[int, Field(ge=1, le=90)]
    scenes: list[str]
    weather_hint: str | None = None
    departure_date: date | None = None
    design_context: DesignContextRef | None = None
    request_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def design_request(self) -> Self:
        if (self.design_context is None) != (self.request_id is None):
            raise ValueError("design_context and request_id must be provided together")
        return self


class UpdatePackingListRequest(BaseModel):
    items: list[PackingItem] | None = None
    status: str | None = None


class PackingPlanItem(BaseModel):
    item_id: str
    role: str
    reason: str


class PackingDailyOutfit(BaseModel):
    day: int
    occasion: str
    item_ids: list[str]
    reasoning: list[str]


class PackingMissingItem(BaseModel):
    category: str
    reason: str


class PackingPlan(BaseModel):
    """BE#68 — rich planner snapshot (present only when ENABLE_PACKING_PLANNER)."""

    summary: str
    weather_summary: str | None = None
    packing_items: list[PackingPlanItem] = []
    daily_outfits: list[PackingDailyOutfit] = []
    missing_items: list[PackingMissingItem] = []


class PackingListResponse(BaseModel):
    packing_list_id: uuid.UUID
    destination: str | None = None
    days: int | None = None
    scenes: list[str]
    departure_date: date | None = None
    items: list[PackingItem]
    status: str
    # BE#68 — additive; None for legacy / planner-off lists (FE ignores it).
    plan: PackingPlan | None = None
    design_context: DesignContextSnapshot | None = None
