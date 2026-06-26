"""Shopping sense-check schemas — M17. V3-style direct return (no envelope)."""

import uuid
from datetime import datetime

from pydantic import BaseModel

from app.common.i18n import I18nMessage


class SenseCheckRequest(BaseModel):
    image_url: str | None = None
    source_url: str | None = None


class SimilarItemResponse(BaseModel):
    item_id: uuid.UUID
    similarity: float


class SenseCheckResponse(BaseModel):
    verdict: str  # buy / consider / skip — stable English enum, FE localizes the tag
    reason: str  # mirrors display_reason.fallback_text (zh-CN); retained for pre-display_reason clients
    # BE#60 — locale-safe structured display field. FE renders from key+params
    # (see sense_check.* keys), never the legacy `reason`.
    display_reason: I18nMessage | None = None
    # Explicit counts so FE can relate "可搭 N 套" to the verdict instead of a
    # bare number. similar_item_count = near-duplicate count behind a skip.
    similar_item_count: int = 0
    similar_items: list[SimilarItemResponse]
    wardrobe_gap: list[str]
    outfit_potential: int  # = pairable_count surfaced to FE


class SenseCheckHistoryItem(BaseModel):
    sense_check_id: uuid.UUID
    verdict: str
    reason: str
    display_reason: I18nMessage | None = None  # BE#60 — see SenseCheckResponse.
    similar_item_count: int = 0
    image_url: str | None
    source_url: str | None
    similar_items: list[SimilarItemResponse]
    wardrobe_gap: list[str]
    outfit_potential: int
    created_at: datetime


class SenseCheckHistoryResponse(BaseModel):
    items: list[SenseCheckHistoryItem]
