from uuid import UUID

from pydantic import BaseModel, Field


class CreateImportTaskRequest(BaseModel):
    input_type: str = Field(
        pattern=(
            "^(image|link|screenshot|image_batch|shared_content|receipt_screenshot|order_screenshot|shopping_email)$"
        )
    )
    source_type: str = Field(pattern="^(camera|album|taobao|jd|xhs|douyin|web|share|screenshot)$")
    image_url: str | None = Field(default=None, min_length=1)
    source_url: str | None = Field(default=None, min_length=1)
    # BE#30 — optional locale hint for the AI display name. When omitted, the
    # worker falls back to the user's persisted settings locale (then zh-CN).
    locale: str | None = Field(default=None, max_length=16)


class CandidateItem(BaseModel):
    title: str
    category: str
    color_main: str
    confidence_score: float
    original_image_url: str
    # BE#30 — locale-aware naming. Optional so old candidate_payloads (OCR
    # imports, in-flight tasks) that predate these keys still deserialize.
    canonical_name: str | None = None
    display_name: str | None = None
    localized_names: dict[str, str] | None = None
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
