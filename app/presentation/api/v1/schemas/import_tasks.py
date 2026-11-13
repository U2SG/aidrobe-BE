from uuid import UUID

from pydantic import BaseModel, Field

from app.common.enums import FitIntent


class CreateImportTaskRequest(BaseModel):
    input_type: str = Field(
        pattern=(
            "^(image|outfit_image|link|screenshot|image_batch|shared_content|"
            "receipt_screenshot|order_screenshot|shopping_email)$"
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
    # Recognised garment attributes (null / [] = unknown). Copied onto the
    # item at confirm; the recommender ranks on formality and seasons.
    formality_score: int | None = None
    season_tags: list[str] = Field(default_factory=list)
    material: str | None = None
    style_tags: list[str] = Field(default_factory=list)
    # Set once this candidate has been confirmed (batches / retries); the
    # client skips it and can resume with the remaining ones.
    confirmed_item_id: UUID | None = None


class ConfirmItemInput(BaseModel):
    """BE#25 — one candidate (keyed by `temp_item_id`) the client elected to
    import, carrying its edited form fields. Typed values win over the AI guess."""

    temp_item_id: str = Field(min_length=1)
    title: str = Field(min_length=1, max_length=120)
    category: str = Field(min_length=1, max_length=32)
    color_main: str = Field(min_length=1, max_length=32)
    location: str | None = Field(default=None, max_length=120)
    size_label: str | None = Field(default=None, max_length=32)
    fit_status: str | None = None
    fit_intent: FitIntent | None = None


class ConfirmItemsRequest(BaseModel):
    items: list[ConfirmItemInput] = Field(min_length=1)
    # Close the task after this batch even if other candidates are left
    # unconfirmed. By default the task stays waiting_confirm until every
    # candidate is confirmed, so the rest can be imported in a later batch.
    dismiss_remaining: bool = False


class ConfirmedItem(BaseModel):
    temp_item_id: str
    item_id: UUID


class ConfirmItemsResponse(BaseModel):
    created_items: list[ConfirmedItem]
    # waiting_confirm while candidates remain; completed otherwise.
    task_status: str | None = None


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
