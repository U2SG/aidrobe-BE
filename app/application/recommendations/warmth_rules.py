"""Common-sense warmth rules: looks nobody would wear, excluded outright.

A sweater or down jacket with shorts, sandals on a cold day, a puffer on a hot
one — these are not "slightly worse" options to down-rank, they are mistakes
that make the whole recommendation look broken. The engine drops such combos
before scoring (falling back to the unfiltered set if nothing survives).

Pure functions over a garment's already-recognised attributes (category,
names, sub_category, material, season_tags). Conservative by design: a trait is
only assigned on explicit evidence, and an item with no evidence has no traits,
so unknown attributes never exclude anything.

Temperatures are the *feels-like* value the engine reasons with (provider
feels-like, shifted by the user's own thermal feedback offset).
"""

from __future__ import annotations

import math
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from app.application.ports.weather import WeatherInfo
from app.common.enums import Category

# Below this feels-like, summer-only pieces (tank, shorts, sandals) are out.
SUMMER_ONLY_MIN_FEELS_LIKE_C = 12.0
# Above this feels-like, winter-only heavy outerwear (down, parka, wool coat) is out.
WINTER_OUTER_MAX_FEELS_LIKE_C = 24.0

# Item traits.
HEAVY_UPPER = "heavy_upper"
SHORT_BOTTOM = "short_bottom"
SUMMER_ONLY = "summer_only"
WINTER_OUTER = "winter_outer"

# Rule ids (recorded in the decision trace).
HEAVY_UPPER_WITH_SHORT_BOTTOM = "heavy_upper_with_short_bottom"
SUMMER_ONLY_WHEN_COLD = "summer_only_when_cold"
WINTER_OUTER_WHEN_HOT = "winter_outer_when_hot"

_UPPER_CATEGORIES = {Category.TOP.value, Category.OUTERWEAR.value}
# English terms match on word boundaries ("tank" ≠ "tankini"); Chinese terms by substring.
_HEAVY_UPPER_EN = (
    "down jacket", "down coat", "down vest", "puffer", "parka", "overcoat",
    "wool coat", "peacoat", "pea coat", "sweater", "jumper", "cable knit",
    "chunky knit", "shearling", "fur coat",
)
_HEAVY_UPPER_ZH = ("羽绒", "棉服", "棉衣", "棉袄", "大衣", "毛呢", "呢子", "毛衣", "羊绒", "羊毛衫", "皮草")
_SHORT_BOTTOM_EN = ("shorts", "mini skirt", "miniskirt", "hot pants")
_SHORT_BOTTOM_ZH = ("短裤", "热裤", "短裙")
_SHORT_BOTTOM_SUB_CATEGORIES = {"shorts", "mini_skirt", "miniskirt", "hot_pants"}
_SUMMER_ONLY_EN = (
    "tank", "tank top", "camisole", "cami", "spaghetti strap", "shorts",
    "hot pants", "sandal", "sandals", "flip flop", "flip flops", "flip-flops",
)
_SUMMER_ONLY_ZH = ("吊带", "背心", "短裤", "热裤", "凉鞋", "人字拖", "凉拖")
# Materials that make an upper heavy on their own: down/cashmere anywhere on the
# upper body; wool only on a top (a wool top is a sweater; a wool blazer is not).
_HEAVY_MATERIALS = {"down", "cashmere"}
_HEAVY_TOP_MATERIALS = {"wool"}


@dataclass(frozen=True)
class WarmthExclusion:
    item_ids: tuple[str, ...]
    rule: str


def feels_like_c(weather: WeatherInfo | None, thermal_offset_c: float = 0.0) -> float | None:
    """The temperature the engine dresses for: provider feels-like (falling back
    to the air temperature when a provider/snapshot has none) plus the user's own
    thermal feedback offset (positive = runs warm). None without weather."""
    if weather is None:
        return None
    base = getattr(weather, "feels_like_c", None)
    if not isinstance(base, int | float) or isinstance(base, bool) or not math.isfinite(base):
        base = weather.temperature_c
    return float(base) + thermal_offset_c


def garment_warmth_traits(
    *,
    category: str,
    names: Iterable[str | None] = (),
    sub_category: str | None = None,
    material: str | None = None,
    season_tags: Sequence[str] = (),
) -> frozenset[str]:
    """Warmth traits of one garment; empty when nothing is known about it."""
    text = " ".join(name.lower() for name in (*names, sub_category) if name)
    material_key = (material or "").strip().lower()
    tags = set(season_tags)
    traits: set[str] = set()

    if category in _UPPER_CATEGORIES and (
        _mentions(text, _HEAVY_UPPER_EN, _HEAVY_UPPER_ZH)
        or material_key in _HEAVY_MATERIALS
        or (category == Category.TOP.value and material_key in _HEAVY_TOP_MATERIALS)
        or tags == {"winter"}
    ):
        traits.add(HEAVY_UPPER)
        if category == Category.OUTERWEAR.value:
            traits.add(WINTER_OUTER)

    if category == Category.BOTTOM.value and (
        (sub_category or "").strip().lower() in _SHORT_BOTTOM_SUB_CATEGORIES
        or _mentions(text, _SHORT_BOTTOM_EN, _SHORT_BOTTOM_ZH)
    ):
        traits.add(SHORT_BOTTOM)

    # A heavy piece is never summer-only, whatever its name ("羽绒背心" is a down vest).
    if HEAVY_UPPER not in traits and (
        _mentions(text, _SUMMER_ONLY_EN, _SUMMER_ONLY_ZH)
        or (category in {Category.BOTTOM.value, Category.SHOES.value} and tags == {"summer"})
    ):
        traits.add(SUMMER_ONLY)
    return frozenset(traits)


def item_warmth_violation(traits: frozenset[str], feels_like_c: float | None) -> str | None:
    """Rule a single garment breaks at this feels-like (None when unknown)."""
    if feels_like_c is None:
        return None
    if SUMMER_ONLY in traits and feels_like_c < SUMMER_ONLY_MIN_FEELS_LIKE_C:
        return SUMMER_ONLY_WHEN_COLD
    if WINTER_OUTER in traits and feels_like_c > WINTER_OUTER_MAX_FEELS_LIKE_C:
        return WINTER_OUTER_WHEN_HOT
    return None


def combo_warmth_violation(
    combo_traits: Sequence[frozenset[str]], feels_like_c: float | None
) -> str | None:
    """First warmth rule this look breaks, or None. The heavy-upper + shorts
    clash holds at any temperature, so it applies even without weather."""
    if any(HEAVY_UPPER in t for t in combo_traits) and any(SHORT_BOTTOM in t for t in combo_traits):
        return HEAVY_UPPER_WITH_SHORT_BOTTOM
    for traits in combo_traits:
        rule = item_warmth_violation(traits, feels_like_c)
        if rule is not None:
            return rule
    return None


def _mentions(text: str, english: Iterable[str], chinese: Iterable[str]) -> bool:
    if not text:
        return False
    if any(term in text for term in chinese):
        return True
    return any(re.search(rf"(?<![a-z]){re.escape(term)}(?![a-z])", text) for term in english)
