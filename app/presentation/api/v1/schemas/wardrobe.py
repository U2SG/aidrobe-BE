from datetime import date
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, Field

from app.common.enums import FitIntent, WardrobeItemStatus


class ConfirmImportRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    category: str = Field(min_length=1, max_length=32)
    color_main: str = Field(min_length=1, max_length=32)
    location: str | None = Field(default=None, max_length=120)
    size_label: str | None = Field(default=None, max_length=32)
    fit_status: str | None = None
    # BE#121 — the fit chips the FE confirm form collects; enum-typed so bad
    # values 422 instead of landing in the column.
    fit_intent: FitIntent | None = None
    # M18 — client may override the OCR-auto-filled values from the candidate.
    purchase_price: Decimal | None = Field(default=None, ge=0)
    purchase_date: date | None = None


class ConfirmImportResponse(BaseModel):
    item_id: UUID


class WardrobeItemResponse(BaseModel):
    item_id: UUID
    title: str
    display_name: str
    # BE#30 — naming provenance + locale variants. Additive: `title` stays the
    # primary display name (user edits win). canonical_name is the stable
    # English AI name; localized_names maps locale -> display name.
    canonical_name: str | None = None
    localized_names: dict[str, str] | None = None
    name_source: str = "ai"
    category: str
    color_main: str
    # A-4: tightened from str to WardrobeItemStatus enum (wearable/unavailable/dirty/lost/donated)
    status: WardrobeItemStatus
    cover_image_url: str | None = None
    original_image_url: str | None = None
    # V4 item cutout lifecycle: skipped | queued | ready | failed. When `ready`,
    # cover_image_url is the product-shot cutout; while `queued` the client can
    # poll + show a "polishing" placeholder.
    cutout_status: str = "skipped"
    location: str | None = None
    size_label: str | None = None
    size_region: str | None = None
    fit_intent: str | None = None
    fit_status: str
    comfort_score: int | None = None
    # V3 M14 extended fields
    sub_category: str | None = None
    color_secondary: str | None = None
    material: str | None = None
    season_tags: list[str] | None = None
    style_tags: list[str] | None = None
    formality_score: int | None = None
    time_cost_level: str | None = None
    # V3 M12 insights
    purchase_price: float | None = None
    wear_count: int = 0
    # A-4: last_worn is read-only here (write path deferred to A-5); purchase_date is writeable
    last_worn: date | None = None
    purchase_date: date | None = None


class WardrobeListResponse(BaseModel):
    items: list[WardrobeItemResponse]
    limit: int
    offset: int
    total: int


class UpdateWardrobeItemRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    category: str | None = Field(default=None, min_length=1, max_length=32)
    color_main: str | None = Field(default=None, min_length=1, max_length=32)
    # A-4: tightened from str | None to WardrobeItemStatus | None.
    # NOTE: transition matrix enforcement is deferred; only enum validation at API boundary.
    status: WardrobeItemStatus | None = None
    location: str | None = Field(default=None, max_length=120)
    # V3 M14 extended fields
    sub_category: str | None = Field(default=None, max_length=32)
    color_secondary: str | None = Field(default=None, max_length=32)
    material: str | None = Field(default=None, max_length=64)
    season_tags: list[str] | None = None
    style_tags: list[str] | None = None
    formality_score: int | None = Field(default=None, ge=1, le=5)
    time_cost_level: str | None = Field(default=None, max_length=16)
    purchase_price: float | None = Field(default=None, ge=0)
    # A-4: purchase_date writeable; last_worn_at write path is A-5
    purchase_date: date | None = None


class DeleteWardrobeItemResponse(BaseModel):
    deleted: bool


class MarkItemWornRequest(BaseModel):
    # Optional backfill date; server defaults to today (UTC) when omitted.
    worn_date: date | None = None


class WearHistoryEntry(BaseModel):
    worn_date: date
    # None for standalone manual wears (no outfit context).
    outfit_id: UUID | None = None
    outfit_type: str | None = None
    # 'outfit' — wear recorded via an outfit; 'manual' — standalone item wear.
    source: str = "outfit"


class ItemWearHistoryResponse(BaseModel):
    item_id: UUID
    days: int
    daily_counts: dict[str, int]  # 'YYYY-MM-DD' -> count
    entries: list[WearHistoryEntry]
