from enum import StrEnum


class ItemStatus(StrEnum):
    WEARABLE = "wearable"
    UNAVAILABLE = "unavailable"


class WardrobeItemStatus(StrEnum):
    WEARABLE = "wearable"
    UNAVAILABLE = "unavailable"
    DIRTY = "dirty"
    LOST = "lost"
    DONATED = "donated"


class ImportStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    WAITING_CONFIRM = "waiting_confirm"
    COMPLETED = "completed"
    FAILED = "failed"


class InputType(StrEnum):
    IMAGE = "image"
    OUTFIT_IMAGE = "outfit_image"
    LINK = "link"
    SCREENSHOT = "screenshot"
    IMAGE_BATCH = "image_batch"
    SHARED_CONTENT = "shared_content"
    RECEIPT_SCREENSHOT = "receipt_screenshot"
    ORDER_SCREENSHOT = "order_screenshot"
    SHOPPING_EMAIL = "shopping_email"


class SourceType(StrEnum):
    CAMERA = "camera"
    ALBUM = "album"
    TAOBAO = "taobao"
    JD = "jd"
    XHS = "xhs"
    DOUYIN = "douyin"
    WEB = "web"
    SHARE = "share"
    SCREENSHOT = "screenshot"


class OutfitType(StrEnum):
    SAFE = "safe"
    FAST = "fast"
    STYLISH = "stylish"


class TimeSensitivity(StrEnum):
    RELAXED = "relaxed"
    STANDARD = "standard"
    RUSHED = "rushed"
    INSTANT = "instant"


class FeedbackType(StrEnum):
    LOVE = "love"
    LIKE = "like"
    NEUTRAL = "neutral"
    DISLIKE = "dislike"


class RecommendationEventType(StrEnum):
    """BE#40 — append-only interaction signals for recommendation learning.

    Distinct from FeedbackType (which is the current-state outfit rating): this
    is the event stream (what the user did, when), the raw material for future
    ranking / preference learning."""

    LIKED = "liked"
    DISLIKED = "disliked"
    NEUTRAL = "neutral"
    SAVED = "saved"
    DISMISSED = "dismissed"
    WORN = "worn"
    EDITED = "edited"
    SHARED = "shared"
    DELETED = "deleted"
    IMPRESSION = "impression"
    # BE#81 — passive-learning signals emitted by V2 (no daily "穿了" button).
    OUTFIT_IMPRESSION = "outfit_impression"
    OUTFIT_SWAPPED = "outfit_swapped"
    OUTFIT_DISLIKED = "outfit_disliked"
    CONTEXT_OVERRIDE_SUBMITTED = "context_override_submitted"
    PASSIVE_WEAR_PROMPT_ANSWERED = "passive_wear_prompt_answered"
    RESURFACE_INSIGHT_VIEWED = "resurface_insight_viewed"
    RESURFACE_ITEM_OPENED = "resurface_item_opened"
    TEMPERATURE_MISMATCH_REPORTED = "temperature_mismatch_reported"


class FitStatus(StrEnum):
    UNKNOWN = "unknown"
    FITS_WELL = "fits_well"
    LOOSE = "loose"
    TIGHT = "tight"
    OVERSIZED_OK = "oversized_ok"
    NEEDS_ALTERATION = "needs_alteration"
    NO_LONGER_FITS = "no_longer_fits"


class FitIntent(StrEnum):
    REGULAR = "regular"
    FITTED = "fitted"
    RELAXED = "relaxed"
    OVERSIZED = "oversized"
    CROPPED = "cropped"
    LONGLINE = "longline"
    WIDE_LEG = "wide_leg"
    STRAIGHT = "straight"
    SLIM = "slim"


class FitFeedbackType(StrEnum):
    FITS_WELL = "fits_well"
    TIGHT = "tight"
    LOOSE = "loose"
    UNCOMFORTABLE = "uncomfortable"
    EASY_TO_MOVE = "easy_to_move"
    NOT_FLATTERING = "not_flattering"


class FitScene(StrEnum):
    COMMUTE = "commute"
    FORMAL = "formal"
    CASUAL = "casual"
    DATE = "date"
    TRAVEL = "travel"


class RecommendationStatus(StrEnum):
    READY = "ready"
    INSUFFICIENT_ITEMS = "insufficient_items"
    FAILED = "failed"


class RecommendationGenerationSource(StrEnum):
    NIGHTLY = "nightly"
    DEPARTURE = "departure"
    READ_TIME = "read_time"
    REFRESH = "refresh"
    CONTEXT_OVERRIDE = "context_override"
    TOMORROW_PREVIEW = "tomorrow_preview"
    MIDDAY_CHECKPOINT = "midday_checkpoint"


class ImageType(StrEnum):
    ORIGINAL = "original"
    STANDARD = "standard"


class CutoutStatus(StrEnum):
    """Lifecycle of an item's gpt-image-2 product-shot cutout.

    `skipped` is the default/rollback state (cutout feature off or no source) —
    the item just shows its original photo. `queued` → `ready`/`failed` is the
    async-job arc; `failed` is set by the dead-letter reconcile so the client
    stops polling.
    """

    SKIPPED = "skipped"
    QUEUED = "queued"
    READY = "ready"
    FAILED = "failed"


class LearnStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    LEARNED = "learned"
    FAILED = "failed"


class Category(StrEnum):
    TOP = "top"
    BOTTOM = "bottom"
    OUTERWEAR = "outerwear"
    DRESS = "dress"
    SHOES = "shoes"


# --- V3 P0 purchase decision (docs/v3-p0-contract.md §5) ------------------
# Literals are frozen by the contract; the persistence CHECK constraints in
# `models/purchase_decision.py` and migration 0046 must stay in sync with them
# (asserted by tests/services/test_v3_decision_migration.py).


class PurchaseCandidateSource(StrEnum):
    """How the user handed us the thing they are considering buying."""

    SHARE_LINK = "share_link"
    SCREENSHOT = "screenshot"
    ORDER_IMAGE = "order_image"
    ASK_LINK = "ask_link"


class PurchaseCandidateStatus(StrEnum):
    """Parsing lifecycle. `parse_failed` is terminal but still decidable —
    the decision then degrades to `insufficient_information` (contract §5.2)."""

    SUBMITTED = "submitted"
    PARSED = "parsed"
    PARSE_FAILED = "parse_failed"


class WardrobeDecisionType(StrEnum):
    """Which question a snapshot answers. P0 only asks the purchase one;
    the column exists because `candidate_id` and `outfit_id` are both
    nullable (V3_SPEC §11.2) and future decision kinds reuse the table."""

    PURCHASE = "purchase"


class PurchaseDecision(StrEnum):
    """The five bounded states — no numeric suitability score (V3_SPEC §5.4)."""

    WORTH_ADDING = "worth_adding"
    CONSIDER_ALTERNATE = "consider_alternate"
    WAIT = "wait"
    AVOID_DUPLICATE = "avoid_duplicate"
    INSUFFICIENT_INFORMATION = "insufficient_information"


class DecisionUncertainty(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


class DecisionVisualNeed(StrEnum):
    """`none` is the "visual need is false" state; the other two only grade
    how prominent the preview entry should be (contract §5.3)."""

    NONE = "none"
    HELPFUL = "helpful"
    RECOMMENDED = "recommended"
