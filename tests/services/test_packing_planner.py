"""Pure-function coverage for the travel packing planner (BE#68).

`build_trip_plan` awaits the real engine but touches no DB, so the acceptance
scenarios run without testcontainers fixtures beyond the autouse truncate.
"""

from __future__ import annotations

import uuid

from app.application.packing.planner import build_trip_plan, parse_weather_hint
from app.application.recommendations.engine import RecommendationEngine
from app.common.enums import ItemStatus
from app.infrastructure.persistence.models.wardrobe import WardrobeItem


def _item(category: str, color: str) -> WardrobeItem:
    return WardrobeItem(
        user_id=uuid.uuid4(),
        title=f"{color} {category}",
        category=category,
        color_main=color,
        status=ItemStatus.WEARABLE.value,
        fit_status="fits_well",
    )


def _full_wardrobe() -> list[WardrobeItem]:
    return [
        _item("top", "white"),
        _item("top", "navy"),
        _item("bottom", "black"),
        _item("bottom", "beige"),
        _item("shoes", "brown"),
        _item("outerwear", "gray"),
    ]


# ── weather hint parsing ─────────────────────────────────────────────
def test_parse_weather_hint_keywords() -> None:
    assert parse_weather_hint(None) is None
    assert parse_weather_hint("pleasant") is None  # no signal
    cold_rain = parse_weather_hint("cold and rainy")
    assert cold_rain is not None
    assert cold_rain.temperature_c <= 10
    assert cold_rain.rain_probability > 0.5


# ── insufficient wardrobe ────────────────────────────────────────────
async def test_insufficient_wardrobe_yields_no_daily_outfits() -> None:
    plan = await build_trip_plan(
        candidate_items=[_item("top", "white")],  # < 3 → engine insufficient
        scenes=["casual"],
        days=3,
        weather=None,
        engine=RecommendationEngine(),
    )
    assert plan.daily_outfits == []
    # The wardrobe gap is surfaced as missing items rather than silently empty.
    assert plan.missing_items


# ── cold / rainy trip ────────────────────────────────────────────────
async def test_cold_rainy_trip_flags_waterproof_shoes_and_reasons() -> None:
    items = [_item("top", "navy"), _item("bottom", "black"), _item("shoes", "white")]  # light shoes
    plan = await build_trip_plan(
        candidate_items=items,
        scenes=["casual"],
        days=2,
        weather=parse_weather_hint("cold rainy"),
        engine=RecommendationEngine(),
    )
    assert plan.weather_summary is not None and "rain" in plan.weather_summary
    assert any(m.category == "waterproof shoes" for m in plan.missing_items)
    assert plan.daily_outfits
    assert any("rain" in r for r in plan.daily_outfits[0].reasoning)


# ── multi-occasion trip ──────────────────────────────────────────────
async def test_multi_occasion_cycles_scenes_per_day() -> None:
    plan = await build_trip_plan(
        candidate_items=_full_wardrobe(),
        scenes=["business", "casual"],
        days=4,
        weather=None,
        engine=RecommendationEngine(),
    )
    assert [d.occasion for d in plan.daily_outfits] == ["business", "casual", "business", "casual"]
    # Packing list is deduped (one row per item, however many days it's worn).
    assert len({p.item_id for p in plan.packing_items}) == len(plan.packing_items)


# ── missing-item detection ───────────────────────────────────────────
async def test_missing_item_when_scene_category_absent() -> None:
    # "business" expects outerwear; this wardrobe has none.
    items = [_item("top", "white"), _item("bottom", "black"), _item("shoes", "brown")]
    plan = await build_trip_plan(
        candidate_items=items,
        scenes=["business"],
        days=1,
        weather=None,
        engine=RecommendationEngine(),
    )
    assert any(m.category == "outerwear" for m in plan.missing_items)


async def test_no_hallucinated_items_in_plan() -> None:
    items = _full_wardrobe()
    owned = {str(i.id) for i in items}
    plan = await build_trip_plan(
        candidate_items=items, scenes=["casual"], days=2, weather=None, engine=RecommendationEngine()
    )
    referenced = {iid for d in plan.daily_outfits for iid in d.item_ids}
    assert referenced and referenced <= owned  # every referenced item is really owned
