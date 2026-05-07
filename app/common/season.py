"""Season derivation. Northern-Hemisphere defaults; Phase D will replace
with location-aware mapping (southern hemisphere, equatorial, etc.)."""

from datetime import date


def current_season(today: date) -> str:
    m = today.month
    if m in (3, 4, 5):
        return "spring"
    if m in (6, 7, 8):
        return "summer"
    if m in (9, 10, 11):
        return "autumn"
    return "winter"
