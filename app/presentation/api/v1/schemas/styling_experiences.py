import uuid
from datetime import date, datetime

from pydantic import BaseModel

from app.application.outfits.experiences import (
    ExperienceFeedback,
    ExperienceKind,
    ExperienceOccasion,
    StylingExperienceInput,
)


class StylingExperienceRequest(StylingExperienceInput):
    pass


class StylingExperienceResponse(BaseModel):
    experience_id: uuid.UUID
    request_id: uuid.UUID
    outfit_id: uuid.UUID
    kind: ExperienceKind
    conversation_id: uuid.UUID | None
    revision: int | None
    worn_date: date | None
    feedback: ExperienceFeedback | None
    note: str | None
    occasion: ExperienceOccasion | None
    supersedes_id: uuid.UUID | None
    created_at: datetime


class StylingExperienceListResponse(BaseModel):
    items: list[StylingExperienceResponse]
    latest_worn_date: date | None


class StylingExperienceDeleteResponse(BaseModel):
    success: bool = True
