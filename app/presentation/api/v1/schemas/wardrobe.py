from uuid import UUID

from pydantic import BaseModel, Field


class ConfirmImportRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    category: str = Field(min_length=1, max_length=32)
    color_main: str = Field(min_length=1, max_length=32)
    location: str | None = Field(default=None, max_length=120)
    size_label: str | None = Field(default=None, max_length=32)
    fit_status: str | None = None


class ConfirmImportResponse(BaseModel):
    item_id: UUID


class WardrobeItemResponse(BaseModel):
    item_id: UUID
    title: str
    category: str
    color_main: str
    status: str
    cover_image_url: str | None = None
    original_image_url: str | None = None
    location: str | None = None
    size_label: str | None = None
    size_region: str | None = None
    fit_intent: str | None = None
    fit_status: str
    comfort_score: int | None = None


class WardrobeListResponse(BaseModel):
    items: list[WardrobeItemResponse]
    limit: int
    offset: int
    total: int


class UpdateWardrobeItemRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=120)
    category: str | None = Field(default=None, min_length=1, max_length=32)
    color_main: str | None = Field(default=None, min_length=1, max_length=32)
    status: str | None = None
    location: str | None = Field(default=None, max_length=120)


class DeleteWardrobeItemResponse(BaseModel):
    deleted: bool
