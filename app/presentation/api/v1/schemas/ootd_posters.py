import uuid

from pydantic import BaseModel, Field, field_validator


class CreateOotdPosterRequest(BaseModel):
    # Either an explicit selfie URL or a wear-history row to pull it from.
    ootd_image_url: str | None = Field(default=None, min_length=1)

    @field_validator("ootd_image_url")
    @classmethod
    def validate_url_scheme(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith(("http://", "https://")):
            raise ValueError("ootd_image_url must be http or https")
        return v
    wear_history_id: uuid.UUID | None = None
    template_id: str = Field(default="minimal", pattern="^(minimal|studio|street)$")
    # Body images leave to a third-party model — the client MUST set this true
    # after showing the consent + "AI preview only" notice.
    consent: bool = False


class CreateOotdPosterResponse(BaseModel):
    asset_id: uuid.UUID
    generation_status: str
    disclaimer: str
