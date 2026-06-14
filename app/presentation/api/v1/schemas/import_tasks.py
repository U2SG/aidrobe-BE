from uuid import UUID

from pydantic import BaseModel, Field


class CreateImportTaskRequest(BaseModel):
    input_type: str = Field(
        pattern=(
            "^(image|link|screenshot|image_batch|shared_content"
            "|receipt_screenshot|order_screenshot|shopping_email)$"
        )
    )
    source_type: str = Field(pattern="^(camera|album|taobao|jd|xhs|douyin|web|share|screenshot)$")
    image_url: str | None = Field(default=None, min_length=1)
    source_url: str | None = Field(default=None, min_length=1)


class CandidateItem(BaseModel):
    title: str
    category: str
    color_main: str
    confidence_score: float
    original_image_url: str
    # M18 — auto-filled from OCR for receipt / order / email imports; null for
    # the vision/link paths. Lets the client pre-populate the confirm form.
    size_label: str | None = None
    purchase_price: str | None = None
    purchase_date: str | None = None


class ImportTaskResponse(BaseModel):
    task_id: UUID
    status: str
    candidate_item: CandidateItem | None = None
    error_code: str | None = None
    error_message: str | None = None
