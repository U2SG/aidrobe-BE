"""Outfit history schemas — M11."""

import uuid
from datetime import date, datetime
from typing import Any

from pydantic import BaseModel


class OutfitSummary(BaseModel):
    outfit_id: uuid.UUID
    outfit_type: str
    title: str
    reason_text: str
    outfit_board_url: str | None = None
    items: list[dict[str, Any]] = []


class OutfitHistoryEntry(BaseModel):
    wear_history_id: uuid.UUID
    worn_date: date
    outfit: OutfitSummary | None = None
    feedback_type: str | None = None
    reason_tags: list[str] = []
    ootd_image_url: str | None = None
    selected_at: datetime


class OutfitHistoryListResponse(BaseModel):
    items: list[OutfitHistoryEntry]
    page: int
    page_size: int
    total: int


class OutfitCalendarResponse(BaseModel):
    dates: list[date]


class OotdUploadRequest(BaseModel):
    ootd_image_url: str


class OotdUploadResponse(BaseModel):
    wear_history_id: uuid.UUID
    ootd_image_url: str
