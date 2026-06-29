"""Travel packing PLANNER (BE#68).

Upgrades the rule-based packing picker into a trip planner: a per-day outfit
plan (via the recommendation engine), a deduped packing list with reuse reasons,
weather/occasion reasoning, and missing-item warnings.

`build_trip_plan` is pure (it awaits the engine but touches no DB) so the
acceptance scenarios — insufficient wardrobe, cold/rainy, multi-occasion,
missing-item — are unit-testable without a database. The use case
(`packing/generate.py`) does the DB loading and hands real wardrobe rows in.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from app.application.packing.generate import _categories_for_scene
from app.application.ports.weather import WeatherInfo
from app.application.recommendations.engine import (
    LIGHT_SHOE_COLORS,
    EngineItem,
    RecommendationEngine,
    RelationshipEdge,
)
from app.common.enums import Category
from app.infrastructure.persistence.models.wardrobe import WardrobeItem

_RAIN_THRESHOLD = 0.5


@dataclass(frozen=True)
class PackingPlanItem:
    item_id: str
    role: str  # the item's category
    reason: str


@dataclass(frozen=True)
class DailyOutfit:
    day: int
    occasion: str
    item_ids: list[str]
    reasoning: list[str]


@dataclass(frozen=True)
class MissingItem:
    category: str
    reason: str


@dataclass(frozen=True)
class TripPlan:
    summary: str
    weather_summary: str | None
    packing_items: list[PackingPlanItem] = field(default_factory=list)
    daily_outfits: list[DailyOutfit] = field(default_factory=list)
    missing_items: list[MissingItem] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_weather_hint(hint: str | None) -> WeatherInfo | None:
    """Coarse-parse the free-text `weather_hint` into a WeatherInfo for engine
    scoring. No forecast API — keyword heuristics only. Returns None when the
    hint carries no usable signal (engine then treats weather as neutral)."""
    if not hint:
        return None
    low = hint.lower()
    cold = any(k in low for k in ("cold", "snow", "freez", "chilly", "冷", "雪"))
    hot = any(k in low for k in ("hot", "warm", "heat", "热", "暖"))
    rainy = any(k in low for k in ("rain", "wet", "storm", "雨"))
    if not (cold or hot or rainy):
        return None
    temperature = 5.0 if cold else 30.0 if hot else 18.0
    rain_probability = 0.8 if rainy else 0.1
    condition = " ".join(
        part for part, on in (("cold", cold), ("hot", hot), ("rainy", rainy)) if on
    )
    return WeatherInfo(
        temperature_c=temperature,
        feels_like_c=temperature,
        rain_probability=rain_probability,
        condition=condition or "mild",
    )


def _weather_summary(weather: WeatherInfo | None) -> str | None:
    if weather is None:
        return None
    parts: list[str] = []
    if weather.temperature_c <= 10:
        parts.append("cold")
    elif weather.temperature_c >= 27:
        parts.append("warm")
    if weather.rain_probability > _RAIN_THRESHOLD:
        parts.append("rain likely")
    return ", ".join(parts) if parts else weather.condition or None


def _to_engine_item(item: WardrobeItem) -> EngineItem:
    return EngineItem(
        item_id=str(item.id),
        title=item.title,
        category=item.category,
        color_main=item.color_main,
        location=item.location,
        fit_status=item.fit_status,
        fit_intent=item.fit_intent,
    )


def _daily_reasoning(
    occasion: str, weather: WeatherInfo | None, *, reuses: bool
) -> list[str]:
    reasoning = [f"suited to {occasion}"]
    if weather is not None:
        if weather.temperature_c <= 10:
            reasoning.append("layered for the cold")
        elif weather.temperature_c >= 27:
            reasoning.append("light enough for the heat")
        if weather.rain_probability > _RAIN_THRESHOLD:
            reasoning.append("holds up in rain")
    if reuses:
        reasoning.append("reuses pieces already packed")
    return reasoning


def _missing_items(
    candidate_items: list[WardrobeItem], scenes: list[str], weather: WeatherInfo | None
) -> list[MissingItem]:
    """Category-coverage gaps per scene + a rain/light-shoe heuristic."""
    owned_categories = {item.category.lower() for item in candidate_items}
    missing: list[MissingItem] = []
    seen: set[str] = set()
    for scene in scenes:
        for category in _categories_for_scene(scene):
            if category not in owned_categories and category not in seen:
                seen.add(category)
                missing.append(
                    MissingItem(
                        category=category,
                        reason=f"No wearable {category} for {scene}",
                    )
                )
    # Rain heuristic (issue example): rain likely but every shoe is light-colored.
    if weather is not None and weather.rain_probability > _RAIN_THRESHOLD:
        shoes = [i for i in candidate_items if i.category.lower() == Category.SHOES.value]
        if shoes and all((s.color_main or "").lower() in LIGHT_SHOE_COLORS for s in shoes):
            missing.append(
                MissingItem(
                    category="waterproof shoes",
                    reason="Rain is likely and current shoes are light-colored",
                )
            )
    return missing


async def build_trip_plan(
    *,
    candidate_items: list[WardrobeItem],
    scenes: list[str],
    days: int,
    weather: WeatherInfo | None,
    engine: RecommendationEngine,
    disliked_colors: set[str] | None = None,
    relationship_edges: list[RelationshipEdge] | None = None,
    style_profile: dict[str, Any] | None = None,
    fit_profile: dict[str, Any] | None = None,
    destination: str | None = None,
) -> TripPlan:
    """Build a per-day outfit plan + packing list + missing-item warnings.

    Each day cycles through `scenes` and asks the engine for the top outfit,
    feeding prior days' signatures so day-to-day looks vary while individual
    pieces still get reused (packing minimalism). Engine `insufficient_items`
    yields an empty plan (the wardrobe can't dress the trip)."""
    occasions = scenes or ["casual"]
    engine_items = [_to_engine_item(i) for i in candidate_items]
    weather_summary = _weather_summary(weather)
    summary = f"{days}-day {destination or 'trip'} packing plan ({', '.join(occasions)})"
    missing = _missing_items(candidate_items, occasions, weather)

    daily_outfits: list[DailyOutfit] = []
    prior_signatures: list[set[str]] = []
    item_use_count: dict[str, int] = {}
    seen_items: set[str] = set()

    for day in range(1, days + 1):
        occasion = occasions[(day - 1) % len(occasions)]
        result = await engine.generate(
            engine_items,
            weather=weather,
            disliked_colors=disliked_colors or set(),
            relationship_edges=relationship_edges or [],
            style_profile=style_profile or {},
            fit_profile=fit_profile or {},
            recent_worn_outfit_item_ids=[set(sig) for sig in prior_signatures],
            avoid_exact_repeats=True,
        )
        if result.status != "ready" or not result.outfits:
            # The wardrobe cannot dress the trip — stop here; missing_items + the
            # (possibly empty) plan tell the user what to add.
            break
        top = result.outfits[0]
        reuses = any(item_id in seen_items for item_id in top.item_ids)
        daily_outfits.append(
            DailyOutfit(
                day=day,
                occasion=occasion,
                item_ids=list(top.item_ids),
                reasoning=_daily_reasoning(occasion, weather, reuses=reuses),
            )
        )
        prior_signatures.append(set(top.item_ids))
        for item_id in top.item_ids:
            item_use_count[item_id] = item_use_count.get(item_id, 0) + 1
            seen_items.add(item_id)

    packing_items = _packing_items(item_use_count, candidate_items, weather)
    return TripPlan(
        summary=summary,
        weather_summary=weather_summary,
        packing_items=packing_items,
        daily_outfits=daily_outfits,
        missing_items=missing,
    )


def _packing_items(
    item_use_count: dict[str, int],
    candidate_items: list[WardrobeItem],
    weather: WeatherInfo | None,
) -> list[PackingPlanItem]:
    by_id = {str(i.id): i for i in candidate_items}
    rainy = weather is not None and weather.rain_probability > _RAIN_THRESHOLD
    packing: list[PackingPlanItem] = []
    # Most-reused pieces first — they earn their luggage space.
    for item_id, count in sorted(item_use_count.items(), key=lambda kv: (-kv[1], kv[0])):
        item = by_id.get(item_id)
        if item is None:
            continue
        reason = f"worn across {count} day{'s' if count != 1 else ''}"
        if rainy and item.category.lower() == Category.OUTERWEAR.value:
            reason += "; handles rain"
        packing.append(PackingPlanItem(item_id=item_id, role=item.category, reason=reason))
    return packing
