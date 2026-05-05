"""Wardrobe insights schemas — M12."""

import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel


class InsightsSummaryResponse(BaseModel):
    total_items: int
    active_rate: float
    idle_count: int
    idle_threshold_days: int
    most_worn_category: str | None = None
    favorite_color: str | None = None


class TopItemResponse(BaseModel):
    item_id: uuid.UUID
    title: str
    category: str
    cover_image_url: str | None = None
    wear_count: int


class IdleItemResponse(BaseModel):
    item_id: uuid.UUID
    title: str
    category: str
    cover_image_url: str | None = None
    wear_count: int
    last_worn: date | None = None


class ColorPaletteResponse(BaseModel):
    palette: dict[str, int]


class StyleTrendEntry(BaseModel):
    year_month: str
    tags: dict[str, int]


class StyleTrendResponse(BaseModel):
    trend: list[StyleTrendEntry]


class CostPerWearItem(BaseModel):
    item_id: uuid.UUID
    title: str
    purchase_price: Decimal | None = None
    wear_count: int
    cost_per_wear: Decimal | None = None
