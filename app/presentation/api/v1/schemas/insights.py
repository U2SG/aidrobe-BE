"""Wardrobe insights schemas — M12."""

import uuid
from datetime import date
from decimal import Decimal

from pydantic import BaseModel, Field


class InsightsSummaryResponse(BaseModel):
    total_items: int
    active_rate: float
    idle_count: int
    idle_threshold_days: int
    most_worn_category: str | None = None
    favorite_color: str | None = None
    wear_history_count: int = Field(
        default=0,
        description=(
            "Sum of item-level wear counts over live items, including manual item wears. "
            "Not the number of outfit records: compare `GET /v1/outfit-history` `total`."
        ),
    )
    has_sufficient_wear_history: bool = False
    report_available: bool = False


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
    prev_total_wears: int
    top_items: list[ReportTopItem]
    top_combos: list[ReportOutfitCombo]
    color_palette: dict[str, int]
    style_keywords: list[str]
    style_trend: ReportStyleTrend | None = None
    highest_cost_per_wear: ReportCostPerWear | None = None
    longest_idle_item: ReportIdleItem | None = None
    recommendation_adoption_rate: float


class WardrobeHealthInsightItem(BaseModel):
    """A single wardrobe-health insight as i18n-safe codes + numbers.

    The backend never returns user-facing copy; the frontend localizes from the
    stable ``type`` code and the numeric ``metrics``.

    ``type`` (stable machine codes):
    - ``wear_coverage`` — share of items worn recently.
      ``metrics``: ``{"coverage_ratio": float (0-1), "window_days": int (90)}``
    - ``unused_items`` — items not worn within rolling windows.
      ``metrics``: ``{"unused_60": int, "unused_90": int, "unused_180": int}``
    - ``category_balance`` — concentration across categories.
      ``metrics``: ``{"dominant_ratio": float (0-1), "counts": {<category>: int}}``;
      empty ``metrics`` ``{}`` when balance can't be computed yet.
    - ``data_sparsity`` — emitted only for an empty wardrobe.
      ``metrics``: ``{"item_count": 0}``.

    ``severity``: stable code ``low`` / ``medium`` / ``high``.
    """

    type: str
    severity: str
    # Numeric metrics for FE-side copy. Scalars, except
    # ``category_balance.counts`` which is a per-category count map.
    metrics: dict[str, int | float | dict[str, int]]


class WardrobeHealthResponse(BaseModel):
    """Wardrobe health score + insights, fully i18n-safe.

    ``score``: int 0-100. ``level``: stable code ``great`` / ``good`` /
    ``fair`` / ``low`` (FE localizes the headline). No baked English summary —
    FE renders copy from ``score``, ``level`` and each insight's ``metrics``.
    """

    score: int
    level: str
    insights: list[WardrobeHealthInsightItem]
