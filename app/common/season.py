"""Season derivation. Northern-Hemisphere defaults; Phase D will replace
with location-aware mapping (southern hemisphere, equatorial, etc.)."""

from datetime import date
from typing import Literal

# Canonical season vocabulary — matches season_tags values persisted by
# the V3 M14 import/update paths and the strings current_season returns.
SeasonName = Literal["spring", "summer", "autumn", "winter"]


def current_season(today: date) -> str:
    m = today.month
    if m in (3, 4, 5):
        return "spring"
    if m in (6, 7, 8):
        return "summer"
    if m in (9, 10, 11):
        return "autumn"
    return "winter"


def season_tags_of(raw: object) -> tuple[str, ...]:
    """Season tags from the stored `{"tags": [...]}` JSONB shape; () if unknown."""
    tags = raw.get("tags") if isinstance(raw, dict) else None
    return tuple(str(tag) for tag in tags) if isinstance(tags, list) else ()


def in_season(tags: tuple[str, ...], season: str) -> bool:
    """True when the garment suits `season`; untagged garments always do."""
    return not tags or season in tags or "all_season" in tags
