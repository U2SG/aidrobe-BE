"""Shopping sense-check schemas — M17. V3-style direct return (no envelope)."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field

from app.common.i18n import I18nMessage


class SenseCheckRequest(BaseModel):
    image_url: str | None = None
    source_url: str | None = None
    locale: str | None = Field(default=None, max_length=16)
    # BE#69 — optional price for the cost-per-wear hint (the item isn't owned yet,
    # so there's no wardrobe purchase_price to read).
    price: float | None = Field(default=None, ge=0)


class SimilarItemResponse(BaseModel):
    item_id: uuid.UUID
    similarity: float
    name: str | None = None
    category: str | None = None
    color_main: str | None = None
    thumbnail_url: str | None = None


# ── BE#69 purchase-evaluator enrichment (all optional / additive) ──────────────
class RedundancySimilarItem(BaseModel):
    item_id: uuid.UUID
    similarity: float
    reason: str


class RedundancyResponse(BaseModel):
    level: str  # low | medium | high
    similar_owned_items: list[RedundancySimilarItem] = []


class OutfitSampleResponse(BaseModel):
    item_ids: list[uuid.UUID]
    reasoning: list[str]


class WardrobeGapDetailResponse(BaseModel):
    category: str
    severity: str  # low | medium | high


class DecisionFactorResponse(BaseModel):
    factor: str  # redundancy | wardrobe_gap | outfit_potential | preference_conflict
    detail: str
    direction: str  # buy | consider | skip | neutral


class PurchaseEvaluation(BaseModel):
    """BE#69 — present only when ENABLE_PURCHASE_EVALUATOR; null/empty otherwise."""

    confidence: float | None = None
    redundancy: RedundancyResponse | None = None
    outfit_samples: list[OutfitSampleResponse] = []
    wardrobe_gap_detail: list[WardrobeGapDetailResponse] = []
    factors: list[DecisionFactorResponse] = []
    cost_per_wear_hint: str | None = None


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
    # BE#69 — wardrobe-aware evaluation; None for legacy / evaluator-off responses.
    evaluation: PurchaseEvaluation | None = None


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
    evaluation: PurchaseEvaluation | None = None  # BE#69
    created_at: datetime


class SenseCheckHistoryResponse(BaseModel):
    items: list[SenseCheckHistoryItem]
