"""Outfit history schemas — M11."""

import uuid
from datetime import date, datetime

from pydantic import BaseModel, Field

from app.presentation.api.v1.schemas.recommendations import RecommendationItemResponse


class OutfitHistoryItem(RecommendationItemResponse):
    cover_image_url: str | None = None


class OutfitSummary(BaseModel):
    outfit_id: uuid.UUID
    outfit_type: str
    title: str
    reason_text: str
    outfit_board_url: str | None = None
    items: list[OutfitHistoryItem] = []


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
    total: int = Field(
        description=(
            "Number of outfit-level wear records (one per outfit worn on a day). "
            "Not comparable to insights `wear_history_count`, which sums item-level "
            "wears including manual item wears."
        )
    )


class OutfitCalendarResponse(BaseModel):
    dates: list[date]


class OotdUploadRequest(BaseModel):
    ootd_image_url: str


class OotdUploadResponse(BaseModel):
    wear_history_id: uuid.UUID
    ootd_image_url: str
