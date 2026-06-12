import uuid

from pydantic import BaseModel, Field


class CreateOotdPosterRequest(BaseModel):
    # Either an explicit selfie URL or a wear-history row to pull it from.
    ootd_image_url: str | None = Field(default=None, min_length=1)
    wear_history_id: uuid.UUID | None = None
    template_id: str = Field(default="minimal", pattern="^(minimal|studio|street)$")
    # Body images leave to a third-party model — the client MUST set this true
    # after showing the consent + "AI preview only" notice.
    consent: bool = False


class CreateOotdPosterResponse(BaseModel):
    asset_id: uuid.UUID
    generation_status: str
    disclaimer: str
