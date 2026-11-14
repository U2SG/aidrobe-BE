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


# --- V3 P0 avatar (docs/v3-p0-contract.md §4) -----------------------------
# Literals are frozen by the contract; the persistence CHECK constraints in
# `models/avatar.py` and migration 0047 are generated from these enums, and
# tests/services/test_v3_avatar_migration.py asserts the two cannot drift.


class AvatarStatus(StrEnum):
    """Profile lifecycle (contract §4.1, V3_SPEC §7.2).

    `none` is a real persisted state, not the absence of a row: a profile is
    created the moment a candidate exists, and `GET /v1/avatar-profile`
    answers 200 with `status=none` rather than 404.
    """

    NONE = "none"
    CANDIDATE = "candidate"
    PREPARING = "preparing"
    READY = "ready"
    REFRESH_RECOMMENDED = "refresh_recommended"
    DISABLED = "disabled"


#: Exactly the two states in which the frozen `GET /v1/avatar-profile` shape
#: carries a non-null `active_version` (contract §4.2). The CHECK constraint on
#: `avatar_profiles` is generated from this set, so the response shape cannot
#: be contradicted by a row.
AVATAR_STATUSES_WITH_ACTIVE_VERSION: frozenset[AvatarStatus] = frozenset(
    {AvatarStatus.READY, AvatarStatus.REFRESH_RECOMMENDED}
)


class AvatarSourceType(StrEnum):
    """Where the person reference material came from (V3_SPEC §11.1, §7.1).

    All four are *contextual* acquisitions — there is no mandatory photo
    onboarding in V3 (V3_SPEC §21 anti-goal). `outfit_import` material still
    requires its own explicit consent: importing clothes never implies
    person-reference consent (contract §4.3, source B rule).
    """

    OOTD_PHOTO = "ootd_photo"
    OUTFIT_IMPORT = "outfit_import"
    USER_UPLOAD = "user_upload"
    CAMERA_CAPTURE = "camera_capture"


#: Source types whose stored object was uploaded **for** the avatar and is
#: therefore avatar-owned: deleting the avatar deletes the object.
#:
#: The other two — `ootd_photo` and `outfit_import` — point at objects that
#: belong to the OOTD / import features. The avatar only ever *referenced*
#: them, so deleting the avatar drops the reference and must NOT delete the
#: user's OOTD photo out of their own history. `AvatarLifecycle.delete_avatar`
#: is built on this distinction.
AVATAR_OWNED_SOURCE_TYPES: frozenset[AvatarSourceType] = frozenset(
    {AvatarSourceType.USER_UPLOAD, AvatarSourceType.CAMERA_CAPTURE}
)


class AvatarConsentStatus(StrEnum):
    """Per-source-asset consent (contract §4.3).

    `declined` is terminal for that asset: "accept=false dismisses that
    candidate permanently — the same asset is never re-prompted"
    (contract §4.2, V3_SPEC §7.3).
    """

    PENDING = "pending"
    ACCEPTED = "accepted"
    DECLINED = "declined"


class AvatarQualityStatus(StrEnum):
    """Whether material is usable as a person reference.

    Deliberately three neutral states with no score. V3-012 owns the detection
    rules; the vocabulary is fixed here so a quality signal can never become a
    judgement about the person (see `AvatarSourceAsset.quality_signals`).
    """

    PENDING = "pending"
    PASSED = "passed"
    REJECTED = "rejected"


# --- V3 P0 static try-on (docs/v3-p0-contract.md §6) ----------------------


class VisualType(StrEnum):
    """What kind of image an `outfit_visual_assets` row holds.

    **A vocabulary, not a constraint.** Unlike the avatar enums above, this one
    generates no CHECK on `outfit_visual_assets.visual_type`: the column is a
    free `String(32)` that M10 rows already populate, and constraining it now
    would be unrelated cleanup with real regression surface for no acceptance
    criterion. What it *does* generate is the `tryon_records_provenance` CHECK,
    so the literal that constraint tests against cannot drift from the literal
    the application writes.

    Two P0 values named by contract §6.3 are deliberately absent:

    - `avatar_canonical` — V3-013 stores canonical renders as columns on
      `avatar_versions` (`canonical_front_object_key`), not as rows in this
      table, so no row ever carries it.
    - `outfit_multiview` — "reserved for P1 and must not appear in P0
      responses". A member here would be the first step of that drift.
    """

    #: M10 — the OOTD poster render.
    OOTD_POSTER = "ootd_poster"
    #: M10 — the flat-lay outfit board.
    OUTFIT_BOARD = "outfit_board"
    #: V3 P0 — a static try-on preview (contract §6.2).
    STATIC_TRYON = "static_tryon"


class PreviewFeedbackVerdict(StrEnum):
    """Contract §6.1's frozen fidelity verdict — three states, no score.

    The vocabulary is adopted verbatim from V3-020's immutable acceptance
    criteria (the same authority order §6.1 states for the decision enums), and
    `aidrobe-FE/src/api/previewFeedbackMapping.ts` already sends these literals
    on the wire. It answers exactly one question — *did this render show me, in
    this garment* — and deliberately splits the two ways it can fail, because
    they point at different fixes: `not_like_me` is an identity/avatar problem
    and `garment_wrong` is a garment-rendering problem. Collapsing them into a
    single "bad" would make V3_SPEC §20's first kill criterion uncomputable.

    There is no positive/negative *score* here and none may be derived: §20's
    metrics are per-verdict rates, kept as inspectable dimensions.
    """

    LOOKS_LIKE_ME = "looks_like_me"
    NOT_LIKE_ME = "not_like_me"
    GARMENT_WRONG = "garment_wrong"


class PreviewFeedbackAspect(StrEnum):
    """Optional detail on *what* missed (contract §6.1's `aspects`).

    A closed five-value list of render attributes the user can point at. Note
    what it is not: `garment_fit` is the user saying the clothing looked wrong
    **on the render**, which is a rendering complaint about an image — it is
    never a size, fit or body claim about the person (V3_SPEC §21), and nothing
    reads it as one. `other` exists so a user who means none of the four is not
    pushed into the closest wrong box, and free text is deliberately absent
    (docs/v3-analytics-events.md §5: an open value space defeats every
    guarantee above it).
    """

    FACE = "face"
    BODY_RATIO = "body_ratio"
    GARMENT_FIT = "garment_fit"
    COLOR = "color"
    OTHER = "other"


class PaidGenerationOutcome(StrEnum):
    """How an admitted paid generation ended, in *internal* accounting only.

    V3-026. This is the one axis that tells the two ways a render can stop
    apart, which V3_SPEC §13.3 needs and acceptance 6 states: a
    quality-gate rejection is a render the provider produced and we chose not
    to show, a technical failure is a render that never arrived. Both are
    invisible on the wire — `generation_status` carries the user-facing state
    (contract §6.2) and nothing here reaches a response.

    A reservation with no outcome is *held*: admitted, work not terminal. The
    absence of a value is the state, so there is no second `status` column
    saying the same thing twice.

    **Which outcomes consume the daily budget** is
    `paid_generation_admission.COUNTED_OUTCOMES`, not this enum's membership:

    - `SUCCEEDED` / `QUALITY_REJECTED` count — a provider call happened and
      produced an image, whether or not the quality gate let it through.
    - `TECHNICAL_FAILURE` does not, because V3-017 acceptance 3 ("failed calls
      do not burn call quota") is a passed criterion and still in force.
    - `ABANDONED` does not, because the reservation bought nothing at all —
      the enqueue lost its race, or the revive it was taken for never opened.
    """

    SUCCEEDED = "succeeded"
    QUALITY_REJECTED = "quality_rejected"
    TECHNICAL_FAILURE = "technical_failure"
    ABANDONED = "abandoned"
