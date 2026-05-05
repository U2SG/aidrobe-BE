from uuid import UUID

from pydantic import BaseModel, Field

from app.common.enums import FitFeedbackType, FitIntent, FitScene, FitStatus


class UpdateFitRequest(BaseModel):
    size_label: str | None = Field(default=None, max_length=32)
    size_region: str | None = Field(default=None, max_length=32)
    fit_intent: FitIntent | None = None
    fit_status: FitStatus | None = None
    comfort_score: int | None = Field(default=None, ge=1, le=5)


class FitFeedbackRequest(BaseModel):
    item_id: UUID
    outfit_id: UUID | None = None
    feedback_type: FitFeedbackType
    scene: FitScene


class FitFeedbackResponse(BaseModel):
    feedback_id: UUID
    item_id: UUID
    outfit_id: UUID | None = None
    feedback_type: FitFeedbackType
    scene: FitScene
    fit_status: FitStatus
