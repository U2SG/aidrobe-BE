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
    # Non-empty when upload_url is a presigned POST (S3 multipart form, e.g. minio).
    # Clients POST multipart/form-data with these fields + the file; empty => PUT.
    fields: dict[str, str] = Field(default_factory=dict)
