import uuid
from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, Field, model_validator


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


class UpdateStyleTagsRequest(BaseModel):
    tags: list[Annotated[str, Field(min_length=1, max_length=50)]] = Field(
        min_length=1, max_length=20
    )
