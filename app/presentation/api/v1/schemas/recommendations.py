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


class OutfitResponse(BaseModel):
    outfit_id: UUID
    type: str
    title: str
    reason: str
    time_sensitivity: str = "standard"
    items: list[RecommendationItemResponse]
    # Phase B explainability — all None when their feature flag is off.
    scores: OutfitScoresResponse | None = None
    obstacles: list[ObstacleResponse] | None = None
    prep_steps: list[I18nMessage] | None = None
    prep_notes: list[I18nMessage] | None = None

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


class FeedbackRequest(BaseModel):
    feedback_type: str = Field(pattern="^(love|like|neutral|dislike)$")
    reason_tags: list[str] = []


class FeedbackResponse(BaseModel):
    success: bool


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
