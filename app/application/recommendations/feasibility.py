from collections.abc import Iterable


def missing_first_outfit_slots(categories: Iterable[str]) -> list[str]:
    """Return missing slots for the closest complete first-outfit path."""
    available = set(categories)
    separates_missing = [
        category for category in ("top", "bottom", "shoes") if category not in available
    ]
    dress_missing = [
        category for category in ("dress", "shoes") if category not in available
    ]

    if not separates_missing or not dress_missing:
        return []
    separates_started = bool(available.intersection({"top", "bottom", "shoes"}))
    dress_started = bool(available.intersection({"dress", "shoes"}))
    if separates_started and not dress_started:
        return separates_missing
    if dress_started and not separates_started:
        return dress_missing
    if not separates_started and not dress_started:
        return separates_missing
    if len(dress_missing) < len(separates_missing):
        return dress_missing
    return separates_missing


def has_first_outfit_core(categories: Iterable[str]) -> bool:
    return not missing_first_outfit_slots(categories)
