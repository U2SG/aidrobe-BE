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


# --- M21: half-year / annual wardrobe report ---


class ReportTopItem(BaseModel):
    item_id: uuid.UUID
    title: str
    category: str
    cover_image_url: str | None = None
    wear_count: int


class ReportOutfitCombo(BaseModel):
    outfit_id: uuid.UUID
    title: str
    wear_count: int


class ReportCostPerWear(BaseModel):
    item_id: uuid.UUID
    title: str
    purchase_price: Decimal
    wear_count: int
    cost_per_wear: Decimal


class ReportIdleItem(BaseModel):
    item_id: uuid.UUID
    title: str
    category: str
    cover_image_url: str | None = None
    last_worn: date | None = None
    idle_days: int | None = None


class ReportStyleTrend(BaseModel):
    period: str
    prev_period: str
    current: dict[str, int]
    previous: dict[str, int]
    rising: list[str]
    fading: list[str]


class WardrobeReportResponse(BaseModel):
    period: str
    label: str
    start_date: date
    end_date: date
    is_empty: bool
    total_wears: int
    top_items: list[ReportTopItem]
    top_combos: list[ReportOutfitCombo]
    color_palette: dict[str, int]
    style_keywords: list[str]
    style_trend: ReportStyleTrend | None = None
    highest_cost_per_wear: ReportCostPerWear | None = None
    longest_idle_item: ReportIdleItem | None = None
    recommendation_adoption_rate: float
