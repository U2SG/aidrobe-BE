from pydantic import BaseModel, Field


class PresignUploadRequest(BaseModel):
    file_name: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=100)
    biz_type: str = Field(default="wardrobe_item")
    max_content_length: int = Field(default=10 * 1024 * 1024, gt=0, le=10 * 1024 * 1024)


class PresignUploadResponse(BaseModel):
    upload_url: str
    file_url: str
    object_key: str
