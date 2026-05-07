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
    LINK = "link"
    SCREENSHOT = "screenshot"
    IMAGE_BATCH = "image_batch"
    SHARED_CONTENT = "shared_content"


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


class ImageType(StrEnum):
    ORIGINAL = "original"
    STANDARD = "standard"


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
