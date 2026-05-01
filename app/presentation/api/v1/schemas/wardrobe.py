from uuid import UUID

from pydantic import BaseModel, Field


class ConfirmImportRequest(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    category: str = Field(min_length=1, max_length=32)
    color_main: str = Field(min_length=1, max_length=32)


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


class DeleteWardrobeItemResponse(BaseModel):
    deleted: bool
