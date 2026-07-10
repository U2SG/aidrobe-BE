from datetime import date
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

from app.common.enums import FitFeedbackType, FitScene
from app.common.i18n import I18nMessage


class RecommendationItemResponse(BaseModel):
    item_id: UUID
    title: str | None = None
    category: str | None = None
    display_role: Literal["top", "outer", "bottom", "shoes", "accessory"] = "accessory"


class OutfitScoresResponse(BaseModel):
    aesthetic: float
    feasibility: float
    fit: float
    total: float
    prep_minutes: int
    rules_version: str


class ObstacleResponse(BaseModel):
    kind: Literal["laundry", "lost", "seasonal", "stale", "weather_mismatch"]
    # weather_mismatch targets the whole outfit rather than a single item;
    # in that case `item_id` is the empty string. Clients should treat an
    # empty UUID-like string as "applies to the look."
    item_id: UUID | str
    severity: Literal["blocking", "warning", "info"]
    title: I18nMessage
    # Phase D — top-3 replacement IDs after wearable filter; absent on
    # stale / weather_mismatch where suggesting a single item is meaningless.
    replacement_candidate_ids: list[UUID] | None = None

    # Allow construction from raw dicts persisted in JSONB.
    model_config = {"extra": "ignore"}


class ReasoningItem(BaseModel):
    """BE#38/#85 — one grounded reason with source provenance.

    `type` keys FE rendering (weather / wear_history / style_match / category / fit).
    `source` maps to the FE source enum:
      weather_rule / wardrobe_history / preference_memory / style_memory /
      indoor_climate_inference / llm.
    `referenced_item_ids` — item UUIDs that directly drove this entry (may be
      empty for whole-look reasons).
    `confidence` — high (rule-derived) | medium (heuristic/statistical) |
      low (llm-authored).

    Old persisted rows (pre-BE#85) lack source/referenced_item_ids/confidence;
    missing keys default to None / [] so legacy dailies never 500.
    """

    type: str
    text: str
    # BE#85 provenance fields — all optional so old rows still parse.
    source: str | None = None
    referenced_item_ids: list[str] = Field(default_factory=list)
    confidence: str | None = None

    model_config = {"extra": "ignore"}


class OutfitResponse(BaseModel):
    outfit_id: UUID
    type: str
    title: str
    reason: str
    time_sensitivity: str = "standard"
    is_favorited: bool = False
    items: list[RecommendationItemResponse]
    # Phase B explainability — all None when their feature flag is off.
    scores: OutfitScoresResponse | None = None
    obstacles: list[ObstacleResponse] | None = None
    prep_steps: list[I18nMessage] | None = None
    prep_notes: list[I18nMessage] | None = None
    # BE#37/#38 — grounded reasoning; None unless ENABLE_TODAY_REASONING is on.
    reasoning: list[ReasoningItem] | None = None

    model_config = {"arbitrary_types_allowed": True}

    @staticmethod
    def _coerce_msg_list(raw: Any) -> list[I18nMessage] | None:
        if raw is None:
            return None
        if not isinstance(raw, list):
            return None
        return [I18nMessage(**m) if isinstance(m, dict) else m for m in raw]


class StyleContextResponse(BaseModel):
    template_id: str
    active_tags: list[str] = []
    confidence: float = 0.0


class WeatherContextResponse(BaseModel):
    temperature_c: float
    feels_like_c: float
    rain_probability: float
    condition: str
    # New (QWeather); optional + default None so legacy snapshots still decode.
    humidity: int | None = None
    wind_dir: str | None = None
    wind_scale: str | None = None
    wind_speed: float | None = None
    icon: str | None = None
    location_name: str | None = None


class TodayContextResponse(BaseModel):
    """Phase D — context snapshot the engine saw at generation time. Surfaced
    only when ENABLE_TODAY_CONTEXT=true; absent on rows generated before
    Phase D landed."""

    weather: WeatherContextResponse | None = None
    scene: str | None = None
    fetched_at: str | None = None


class TodayRecommendationResponse(BaseModel):
    recommendation_date: date
    status: str
    guidance_text: str | None = None
    outfits: list[OutfitResponse] = []
    style_context: StyleContextResponse | None = None  # V3 M13
    context: TodayContextResponse | None = None  # Phase D — gated by ENABLE_TODAY_CONTEXT


class WearOutfitRequest(BaseModel):
    worn_date: date | None = None


class WearOutfitResponse(BaseModel):
    success: bool


class FavoriteOutfitResponse(BaseModel):
    success: bool


class ReplacementCandidateResponse(BaseModel):
    item_id: UUID
    title: str
    category: str
    color_main: str
    thumbnail_url: str | None = None
    similarity: float


class ReplacementCandidatesResponse(BaseModel):
    replacements: list[ReplacementCandidateResponse]


class FeedbackRequest(BaseModel):
    feedback_type: str = Field(pattern="^(love|like|neutral|dislike)$")
    reason_tags: list[str] = []


class FeedbackResponse(BaseModel):
    success: bool


class RecommendationEventRequest(BaseModel):
    """BE#81 — general passive-learning event ingestion. Append-only capture;
    it never mutates explicit preferences (OutfitFeedback / FitFeedback)."""

    model_config = {"extra": "ignore"}

    event_type: str = Field(
        pattern=(
            "^(outfit_impression|outfit_swapped|outfit_disliked|context_override_submitted"
            "|passive_wear_prompt_answered|resurface_insight_viewed|resurface_item_opened"
            "|temperature_mismatch_reported)$"
        )
    )
    signal_strength: str = Field(pattern="^(weak|medium|strong)$")
    source_surface: str = Field(pattern="^(today|ask|wardrobe|profile)$")
    outfit_id: UUID | None = None
    recommendation_id: UUID | None = None
    item_id: UUID | None = None
    metadata: dict[str, Any] | None = None


class RecommendationEventResponse(BaseModel):
    event_id: UUID


class OutfitHistoryItemResponse(BaseModel):
    outfit_id: UUID
    worn_date: date
    outfit_type: str
    title: str
    feedback_type: str | None = None
    reason_tags: list[str] = []


class OutfitHistoryResponse(BaseModel):
    items: list[OutfitHistoryItemResponse]
    limit: int
    offset: int


class FitFeedbackBundleItem(BaseModel):
    item_id: UUID
    feedback_type: FitFeedbackType
    scene: FitScene | None = None
    comfort_score: int | None = Field(default=None, ge=1, le=5)


class FeedbackBundleRequest(BaseModel):
    overall: FeedbackRequest
    items: list[FitFeedbackBundleItem] = Field(default_factory=list)


class FeedbackBundleResponse(BaseModel):
    written: int  # total rows written (1 outfit feedback + N fit feedbacks)


class FeedbackTagResponse(BaseModel):
    key: str
    label_zh_cn: str
    applies_to: list[Literal["overall", "item"]]


class FeedbackTagsResponse(BaseModel):
    scene: str | None = None
    tags: list[FeedbackTagResponse]


class FirstOutfitGateResponse(BaseModel):
    state: Literal["no_outfit", "single_outfit", "ready"]
    feasible_outfit_count: int
    missing_slots: list[str]
    first_outfit: OutfitResponse | None
    gate_failures: list[str]
