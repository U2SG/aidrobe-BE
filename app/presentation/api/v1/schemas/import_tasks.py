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
    # BE#24 — multi-candidate fields. All optional with defaults so legacy
    # single-candidate payloads (and OCR payloads) still deserialize into a
    # length-1 candidate_items list. `temp_item_id` is a stable per-candidate
    # id the worker persists (None for legacy rows); #25 confirm-items keys off
    # it. `should_import` marks the recommended candidate; the first one with
    # it true becomes the compat `candidate_item`.
    temp_item_id: str | None = None
    sub_category: str | None = None
    crop_image_url: str | None = None
    cutout_image_url: str | None = None
    bbox: list[float] | None = None
    visibility: str | None = None
    should_import: bool | None = None


class ImportTaskResponse(BaseModel):
    task_id: UUID
    status: str
    # Compat — old FE reads the single recommended candidate here. For a
    # multi-candidate task this mirrors the first `should_import` candidate
    # (falling back to the first overall).
    candidate_item: CandidateItem | None = None
    # BE#24 — new FE reads the full 1–N candidate list here. Always present;
    # empty for processing/failed tasks.
    candidate_items: list[CandidateItem] = []
    error_code: str | None = None
    error_message: str | None = None
