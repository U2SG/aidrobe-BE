"""Shopping sense-check schemas — M17. V3-style direct return (no envelope)."""

import uuid
from datetime import datetime

from pydantic import BaseModel


class SenseCheckRequest(BaseModel):
    image_url: str | None = None
    source_url: str | None = None


class SimilarItemResponse(BaseModel):
    item_id: uuid.UUID
    similarity: float


class SenseCheckResponse(BaseModel):
    verdict: str  # buy / consider / skip
    reason: str
    similar_items: list[SimilarItemResponse]
    wardrobe_gap: list[str]
    outfit_potential: int


class SenseCheckHistoryItem(BaseModel):
    sense_check_id: uuid.UUID
    verdict: str
    reason: str
    image_url: str | None
    source_url: str | None
    similar_items: list[SimilarItemResponse]
    wardrobe_gap: list[str]
    outfit_potential: int
    created_at: datetime


class SenseCheckHistoryResponse(BaseModel):
    items: list[SenseCheckHistoryItem]
