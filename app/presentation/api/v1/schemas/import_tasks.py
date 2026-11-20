from datetime import date
from decimal import Decimal
from typing import Literal
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
    # Ask conversation the photo was attached in; must belong to the caller.
    # GET /v1/ask/conversations/{id} lists the linked tasks under `import_tasks`.
    conversation_id: UUID | None = None
    # Album scan: client-chosen id grouping the photos of one scan, and the
    # photo's capture date (EXIF, user's calendar day; never in the future).
    batch_id: UUID | None = None
    taken_at: date | None = None


class ApplyImportNoteRequest(BaseModel):
    """The user's words about the photo ("只要外套，399 买的，羊毛"), applied to
    the waiting candidates as prefilled include / title / price / material."""

    note: str = Field(min_length=1, max_length=500)


class ImportDuplicateHint(BaseModel):
    item_id: UUID
    title: str
    image_url: str | None = None


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
    # Stable ID shared by both confirmation routes, including old records.
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
    possible_duplicates: list[ImportDuplicateHint] = Field(default_factory=list)


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
    # Omitted = the candidate's value (OCR or an applied note), like size_label.
    purchase_price: Decimal | None = Field(default=None, ge=0, max_digits=10, decimal_places=2)
    material: str | None = Field(default=None, min_length=1, max_length=32)


class ConfirmItemsRequest(BaseModel):
    items: list[ConfirmItemInput]
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
    # Compatibility field for clients using the single-confirm route.
    candidate_item: CandidateItem | None = None
    # BE#24 — new FE reads the full 1–N candidate list here. Always present;
    # empty for processing/failed tasks.
    candidate_items: list[CandidateItem] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    retryable: bool | None = None


class ImportBatchMember(BaseModel):
    task_id: UUID
    temp_item_id: str
    # Verified garment crop (signed, short-lived); null when none could be verified.
    crop_url: str | None = None
    # The whole photo, for members without a crop.
    photo_url: str | None = None
    taken_at: date | None = None


class ImportBatchExistingItem(BaseModel):
    item_id: UUID
    title: str
    image_url: str | None = None


class ImportBatchGroup(BaseModel):
    """One garment seen in one or more photos of the batch."""

    group_id: UUID
    # new: nothing like it in the wardrobe; existing: verified to be existing_item;
    # uncertain: a look-alike (existing_item) or a garment that could not be verified.
    kind: Literal["new", "existing", "uncertain"]
    # pending: undecided; confirmed: in the wardrobe as existing_item; skipped: dismissed.
    status: Literal["pending", "confirmed", "skipped"]
    category: str
    color_main: str
    title: str
    best_crop_url: str | None = None
    members: list[ImportBatchMember]
    existing_item: ImportBatchExistingItem | None = None
    wear_dates: list[date] = Field(default_factory=list)


class ImportBatchResponse(BaseModel):
    batch_id: UUID
    total_photos: int
    # Photos still being recognised or cropped; poll until 0.
    processing: int
    failed: int
    # False while photos are processing or garment comparisons are still outstanding:
    # groups can still merge, so poll again.
    complete: bool
    groups: list[ImportBatchGroup]


class ImportBatchGroupDecision(BaseModel):
    group_id: UUID
    action: Literal["create", "link", "skip"]
    # create only: edited fields; default to the group's recognised values.
    title: str | None = Field(default=None, min_length=1, max_length=120)
    category: str | None = Field(default=None, min_length=1, max_length=32)
    color_main: str | None = Field(default=None, min_length=1, max_length=32)
    # Record a wear on every wear_date of the group (idempotent per day).
    backfill_wear: bool = False


class ConfirmImportBatchRequest(BaseModel):
    groups: list[ImportBatchGroupDecision] = Field(min_length=1, max_length=200)


class ImportBatchGroupResult(BaseModel):
    group_id: UUID
    action: Literal["create", "link", "skip"]
    item_id: UUID | None = None
    # False when the item already existed (linked, or replay of an earlier create).
    created: bool = False
    wears_recorded: int = 0


class ConfirmImportBatchResponse(BaseModel):
    groups: list[ImportBatchGroupResult]
