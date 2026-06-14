import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, field_validator, model_validator


class AddInspirationRequest(BaseModel):
    image_url: str | None = Field(default=None, min_length=1, max_length=1024)
    source_url: str | None = Field(default=None, min_length=1, max_length=1024)

    @model_validator(mode="after")
    def require_at_least_one_url(self) -> "AddInspirationRequest":
        if self.image_url is None and self.source_url is None:
            raise ValueError("image_url or source_url is required")  # noqa: TRY301
        return self


class InspirationResponse(BaseModel):
    id: uuid.UUID
    image_url: str | None
    source_url: str | None = None
    source_type: str
    learn_status: str = "learned"
    created_at: datetime


class InspirationListResponse(BaseModel):
    items: list[InspirationResponse]
    limit: int
    offset: int


class BatchAddInspirationRequest(BaseModel):
    urls: list[Annotated[str, Field(min_length=1, max_length=1024)]] = Field(
        min_length=1, max_length=20
    )


class BatchInspirationResponse(BaseModel):
    items: list[InspirationResponse]


class PdfStyleSourceRequest(BaseModel):
    # M19: PDF / magazine multi-page style ingest. input_type is fixed to
    # pdf_magazine for now (the only async multi-file source); kept explicit so
    # the contract can grow other source types later without a breaking change.
    file_url: str = Field(min_length=1, max_length=1024)
    input_type: str = Field(default="pdf_magazine", pattern="^pdf_magazine$")
    title: str | None = Field(default=None, max_length=255)

    @field_validator("file_url")
    @classmethod
    def validate_file_url_scheme(cls, v: str | None) -> str | None:
        if v is not None and not v.startswith(("http://", "https://")):
            raise ValueError("file_url must be http or https")
        return v


class PdfStyleSourceResponse(BaseModel):
    source_id: uuid.UUID
    status: str = "queued"
    # Best-effort: the real page count is only known after the worker downloads
    # and rasterizes the PDF, so this is 0 at enqueue time.
    estimated_pages: int = 0


class UpdateStyleTagsRequest(BaseModel):
    tags: list[Annotated[str, Field(min_length=1, max_length=50)]] = Field(
        min_length=1, max_length=20
    )
