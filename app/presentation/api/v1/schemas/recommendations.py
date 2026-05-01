from datetime import date
from uuid import UUID

from pydantic import BaseModel, Field


class RecommendationItemResponse(BaseModel):
    item_id: UUID
    title: str | None = None
    category: str | None = None


class OutfitResponse(BaseModel):
    outfit_id: UUID
    type: str
    title: str
    reason: str
    items: list[RecommendationItemResponse]


class TodayRecommendationResponse(BaseModel):
    recommendation_date: date
    status: str
    guidance_text: str | None = None
    outfits: list[OutfitResponse] = []


class WearOutfitRequest(BaseModel):
    worn_date: date | None = None


class WearOutfitResponse(BaseModel):
    success: bool


class FeedbackRequest(BaseModel):
    feedback_type: str = Field(pattern="^(like|dislike)$")


class FeedbackResponse(BaseModel):
    success: bool
