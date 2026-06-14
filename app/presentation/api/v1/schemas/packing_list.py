"""Packing list (M16) schemas — V3-style direct return (no envelope)."""

import uuid
from datetime import date
from typing import Annotated

from pydantic import BaseModel, Field


class PackingItem(BaseModel):
    item_id: uuid.UUID
    day: int
    scene: str
    notes: str = ""


class CreatePackingListRequest(BaseModel):
    destination: str | None = None
    days: Annotated[int, Field(ge=1, le=90)]
    scenes: list[str]
    weather_hint: str | None = None
    departure_date: date | None = None


class UpdatePackingListRequest(BaseModel):
    items: list[PackingItem] | None = None
    status: str | None = None


class PackingListResponse(BaseModel):
    packing_list_id: uuid.UUID
    destination: str | None = None
    days: int | None = None
    scenes: list[str]
    departure_date: date | None = None
    items: list[PackingItem]
    status: str
