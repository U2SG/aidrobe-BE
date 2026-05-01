from enum import StrEnum


class ItemStatus(StrEnum):
    WEARABLE = "wearable"
    UNAVAILABLE = "unavailable"


class ImportStatus(StrEnum):
    WAITING_CONFIRM = "waiting_confirm"
    COMPLETED = "completed"
    FAILED = "failed"


class OutfitType(StrEnum):
    SAFE = "safe"
    FAST = "fast"
    STYLISH = "stylish"


class FeedbackType(StrEnum):
    LIKE = "like"
    DISLIKE = "dislike"


class RecommendationStatus(StrEnum):
    READY = "ready"
    INSUFFICIENT_ITEMS = "insufficient_items"
    FAILED = "failed"


class ImageType(StrEnum):
    ORIGINAL = "original"
    STANDARD = "standard"


class Category(StrEnum):
    TOP = "top"
    BOTTOM = "bottom"
    OUTERWEAR = "outerwear"
    DRESS = "dress"
    SHOES = "shoes"
