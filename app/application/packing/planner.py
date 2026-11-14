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
from app.common.season import season_tags_of
from app.infrastructure.persistence.models.wardrobe import WardrobeItem

_RAIN_THRESHOLD = 0.5

# Plan copy is rendered in the user's locale at generation time and frozen into
# `plan_json` (same approach as purchase_decision_rules).
_TEXTS: dict[str, dict[str, str]] = {
    "en-US": {
        "cold": "cold",
        "warm": "warm",
        "rain_likely": "rain likely",
        "list_sep": ", ",
        "trip": "trip",
        "summary": "{days}-day {destination} packing plan ({scenes})",
        "suited": "suited to {occasion}",
        "layered_cold": "layered for the cold",
        "light_heat": "light enough for the heat",
        "holds_rain": "holds up in rain",
        "reuses": "reuses pieces already packed",
        "no_wearable": "No wearable {category} for {scene}",
        "rain_shoes": "Rain is likely and current shoes are light-colored",
        "worn_day": "worn across {count} day",
        "worn_days": "worn across {count} days",
        "handles_rain": "; handles rain",
    },
    "zh-CN": {
        "cold": "偏冷",
        "warm": "偏热",
        "rain_likely": "可能有雨",
        "list_sep": "，",
        "trip": "出行",
        "summary": "{days}天{destination}行李清单（{scenes}）",
        "suited": "适合{occasion}",
        "layered_cold": "叠穿保暖",
        "light_heat": "轻薄透气，适合炎热天气",
        "holds_rain": "雨天也能应对",
        "reuses": "复用已打包的单品",
        "no_wearable": "{scene}缺少可穿的{category}",
        "rain_shoes": "可能有雨，现有鞋子颜色偏浅",
        "worn_day": "{count}天都会穿到",
        "worn_days": "{count}天都会穿到",
        "handles_rain": "；可应对雨天",
    },
}
_SCENE_LABELS_ZH = {
    "business": "商务",
    "dinner": "聚餐",
    "casual": "休闲",
    "sport": "运动",
    "beach": "海滩",
    "formal": "正式场合",
    "commute": "通勤",
    "travel": "旅行",
    "date": "约会",
    "leisure": "休闲",
}
_CATEGORY_LABELS_ZH = {
    "top": "上装",
    "bottom": "下装",
    "outerwear": "外套",
    "dress": "连衣裙",
    "shoes": "鞋子",
}


def _scene_label(scene: str, locale: str) -> str:
    return _SCENE_LABELS_ZH.get(scene.lower(), scene) if locale == "zh-CN" else scene


def _category_label(category: str, locale: str) -> str:
    return _CATEGORY_LABELS_ZH.get(category, category) if locale == "zh-CN" else category


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


def _weather_summary(weather: WeatherInfo | None, locale: str) -> str | None:
    if weather is None:
        return None
    t = _TEXTS[locale]
    parts: list[str] = []
    if weather.temperature_c <= 10:
        parts.append(t["cold"])
    elif weather.temperature_c >= 27:
        parts.append(t["warm"])
    if weather.rain_probability > _RAIN_THRESHOLD:
        parts.append(t["rain_likely"])
    return t["list_sep"].join(parts) if parts else weather.condition or None


def _to_engine_item(item: WardrobeItem) -> EngineItem:
    return EngineItem(
        item_id=str(item.id),
        title=item.title,
        category=item.category,
        color_main=item.color_main,
        location=item.location,
        fit_status=item.fit_status,
        fit_intent=item.fit_intent,
        sub_category=item.sub_category,
        formality_score=item.formality_score,
        season_tags=season_tags_of(item.season_tags),
    )


def _daily_reasoning(
    occasion: str, weather: WeatherInfo | None, *, reuses: bool, locale: str
) -> list[str]:
    t = _TEXTS[locale]
    reasoning = [t["suited"].format(occasion=_scene_label(occasion, locale))]
    if weather is not None:
        if weather.temperature_c <= 10:
            reasoning.append(t["layered_cold"])
        elif weather.temperature_c >= 27:
            reasoning.append(t["light_heat"])
        if weather.rain_probability > _RAIN_THRESHOLD:
            reasoning.append(t["holds_rain"])
    if reuses:
        reasoning.append(t["reuses"])
    return reasoning


def _missing_items(
    candidate_items: list[WardrobeItem],
    scenes: list[str],
    weather: WeatherInfo | None,
    locale: str,
) -> list[MissingItem]:
    """Category-coverage gaps per scene + a rain/light-shoe heuristic."""
    t = _TEXTS[locale]
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
                        reason=t["no_wearable"].format(
                            category=_category_label(category, locale),
                            scene=_scene_label(scene, locale),
                        ),
                    )
                )
    # Rain heuristic (issue example): rain likely but every shoe is light-colored.
    if weather is not None and weather.rain_probability > _RAIN_THRESHOLD:
        shoes = [i for i in candidate_items if i.category.lower() == Category.SHOES.value]
        if shoes and all((s.color_main or "").lower() in LIGHT_SHOE_COLORS for s in shoes):
            missing.append(
                MissingItem(
                    category="waterproof shoes",
                    reason=t["rain_shoes"],
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
    daily_weather: list[WeatherInfo | None] | None = None,
    locale: str = "en-US",
) -> TripPlan:
    """Build a per-day outfit plan + packing list + missing-item warnings.

    Each day cycles through `scenes` and asks the engine for the top outfit,
    feeding prior days' signatures so day-to-day looks vary while individual
    pieces still get reused (packing minimalism). Engine `insufficient_items`
    yields an empty plan (the wardrobe can't dress the trip).

    `daily_weather` (BE#118) carries real per-day destination forecasts, one
    entry per trip day (None where the provider had no data). Each day falls
    back to the trip-wide `weather` (the parsed hint); the rain/summary
    heuristics use the wettest forecast day so a mid-trip storm still surfaces."""

    def weather_for(day: int) -> WeatherInfo | None:
        if daily_weather and 0 <= day - 1 < len(daily_weather) and daily_weather[day - 1] is not None:
            return daily_weather[day - 1]
        return weather

    # Trip-wide heuristics (summary, rain warnings) key off the wettest day the
    # forecast knows about, not just the hint.
    forecast_days = [w for w in (daily_weather or []) if w is not None]
    trip_weather = max(forecast_days, key=lambda w: w.rain_probability, default=weather)

    occasions = scenes or ["casual"]
    engine_items = [_to_engine_item(i) for i in candidate_items]
    t = _TEXTS[locale]
    weather_summary = _weather_summary(trip_weather, locale)
    summary = t["summary"].format(
        days=days,
        destination=destination or t["trip"],
        scenes=t["list_sep"].join(_scene_label(o, locale) for o in occasions),
    )
    missing = _missing_items(candidate_items, occasions, trip_weather, locale)

    daily_outfits: list[DailyOutfit] = []
    prior_signatures: list[set[str]] = []
    item_use_count: dict[str, int] = {}
    seen_items: set[str] = set()

    for day in range(1, days + 1):
        occasion = occasions[(day - 1) % len(occasions)]
        result = await engine.generate(
            engine_items,
            weather=weather_for(day),
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
                reasoning=_daily_reasoning(occasion, weather_for(day), reuses=reuses, locale=locale),
            )
        )
        prior_signatures.append(set(top.item_ids))
        for item_id in top.item_ids:
            item_use_count[item_id] = item_use_count.get(item_id, 0) + 1
            seen_items.add(item_id)

    packing_items = _packing_items(item_use_count, candidate_items, trip_weather, locale)
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
    locale: str,
) -> list[PackingPlanItem]:
    t = _TEXTS[locale]
    by_id = {str(i.id): i for i in candidate_items}
    rainy = weather is not None and weather.rain_probability > _RAIN_THRESHOLD
    packing: list[PackingPlanItem] = []
    # Most-reused pieces first — they earn their luggage space.
    for item_id, count in sorted(item_use_count.items(), key=lambda kv: (-kv[1], kv[0])):
        item = by_id.get(item_id)
        if item is None:
            continue
        reason = t["worn_day" if count == 1 else "worn_days"].format(count=count)
        if rainy and item.category.lower() == Category.OUTERWEAR.value:
            reason += t["handles_rain"]
        packing.append(PackingPlanItem(item_id=item_id, role=item.category, reason=reason))
    return packing
