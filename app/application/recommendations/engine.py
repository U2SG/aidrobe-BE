from dataclasses import dataclass
from datetime import date
from typing import Any

from app.application.ports.weather import WeatherInfo
from app.application.recommendations.colors import color_score
from app.application.style.build_style_profile import aesthetic_score as compute_aesthetic_score
from app.common.enums import Category, OutfitType

LIGHT_SHOE_COLORS = {
    "white",
    "ivory",
    "cream",
    "beige",
    "light gray",
    "light grey",
    "silver",
    "pink",
    "yellow",
}
FIT_DOWNGRADE_STATUSES = {"tight", "needs_alteration"}
NO_LONGER_FITS = "no_longer_fits"
REPETITION_SIMILARITY_THRESHOLD = 0.6
REPETITION_PENALTY = 0.2


@dataclass(frozen=True)
class EngineItem:
    item_id: str
    title: str
    category: str
    color_main: str
    location: str | None = None
    fit_status: str = "unknown"


@dataclass(frozen=True)
class EngineOutfit:
    outfit_type: str
    title: str
    reason_text: str
    item_ids: list[str]
    score_total: float
    score_breakdown: dict[str, float]


@dataclass(frozen=True)
class EngineResult:
    status: str
    outfits: list[EngineOutfit]
    guidance_text: str | None = None


class RecommendationEngine:
    def generate(
        self,
        items: list[EngineItem],
        recent_worn_main_item_ids: set[str] | None = None,
        target_date: date | None = None,
        style_profile: dict[str, Any] | None = None,
        weather: WeatherInfo | None = None,
        recent_worn_outfit_item_ids: list[set[str]] | None = None,
    ) -> EngineResult:
        recent_worn_main_item_ids = recent_worn_main_item_ids or set()
        recent_worn_outfit_item_ids = recent_worn_outfit_item_ids or []
        wearable = [item for item in items if item.fit_status != NO_LONGER_FITS]
        if len(wearable) < 3:
            return EngineResult(
                status="insufficient_items",
                outfits=[],
                guidance_text="再加一件下装就能搭",
            )

        candidates = self._build_candidates(
            wearable,
            recent_worn_main_item_ids,
            style_profile or {},
            weather,
            recent_worn_outfit_item_ids,
        )
        if not candidates:
            return EngineResult(
                status="insufficient_items",
                outfits=[],
                guidance_text="再加一件下装就能搭",
            )

        selected: list[EngineOutfit] = []
        used_main_items: set[str] = set()
        for outfit_type in [OutfitType.SAFE.value, OutfitType.FAST.value, OutfitType.STYLISH.value]:
            matching = [
                candidate
                for candidate in candidates
                if candidate.outfit_type == outfit_type
                and self._main_item(candidate.item_ids, wearable) not in used_main_items
            ]
            if not matching:
                continue
            best = max(matching, key=lambda candidate: candidate.score_total)
            selected.append(best)
            main_item = self._main_item(best.item_ids, wearable)
            if main_item:
                used_main_items.add(main_item)

        if not selected and candidates:
            selected.append(max(candidates, key=lambda candidate: candidate.score_total))

        return EngineResult(status="ready", outfits=selected[:3])

    def _build_candidates(
        self,
        items: list[EngineItem],
        recent_worn_main_item_ids: set[str],
        style_profile: dict[str, Any],
        weather: WeatherInfo | None,
        recent_worn_outfit_item_ids: list[set[str]],
    ) -> list[EngineOutfit]:
        tops = [item for item in items if item.category == Category.TOP.value]
        bottoms = [item for item in items if item.category == Category.BOTTOM.value]
        dresses = [item for item in items if item.category == Category.DRESS.value]
        outerwears = [item for item in items if item.category == Category.OUTERWEAR.value]
        shoes = [item for item in items if item.category == Category.SHOES.value]
        candidates: list[EngineOutfit] = []

        combos: list[list[EngineItem]] = []
        for top in tops:
            for bottom in bottoms:
                combos.append([top, bottom])
        for dress in dresses:
            combos.append([dress])
        if not combos and items:
            combos.append(items[: min(len(items), 3)])

        if outerwears:
            combos = combos + [
                combo + [outerwears[0]]
                for combo in combos
                if all(item.category != Category.OUTERWEAR.value for item in combo)
            ]

        if shoes:
            combos = [
                combo + [shoes[0]]
                if all(item.category != Category.SHOES.value for item in combo)
                else combo
                for combo in combos
            ]

        for combo in combos:
            for outfit_type in [
                OutfitType.SAFE.value,
                OutfitType.FAST.value,
                OutfitType.STYLISH.value,
            ]:
                candidates.append(
                    self._score_combo(
                        combo,
                        outfit_type,
                        recent_worn_main_item_ids,
                        style_profile,
                        weather,
                        recent_worn_outfit_item_ids,
                    )
                )
        return candidates

    def _score_combo(
        self,
        combo: list[EngineItem],
        outfit_type: str,
        recent_worn_main_item_ids: set[str],
        style_profile: dict[str, Any],
        weather: WeatherInfo | None,
        recent_worn_outfit_item_ids: list[set[str]],
    ) -> EngineOutfit:
        colors = [item.color_main for item in combo]
        categories = [item.category for item in combo]
        utility_score = color_score(colors)
        main_item_id = self._main_item([item.item_id for item in combo], combo)
        freshness_score = 0.3 if main_item_id in recent_worn_main_item_ids else 1.0
        aesthetic_score = compute_aesthetic_score(colors, categories, style_profile)
        time_cost_score = self._time_cost_score(combo)
        weather_score = self._weather_score(combo, weather)
        fit_score = self._fit_score(combo)
        repetition_penalty = self._repetition_penalty(combo, recent_worn_outfit_item_ids)
        type_boost = {
            OutfitType.SAFE.value: 0.05,
            OutfitType.FAST.value: 0.03,
            OutfitType.STYLISH.value: 0.01,
        }[outfit_type]

        has_profile = bool(
            style_profile.get("color_weights") or style_profile.get("category_weights")
        )
        if has_profile:
            total = (
                utility_score * 0.25
                + time_cost_score * 0.20
                + aesthetic_score * 0.20
                + weather_score * 0.15
                + freshness_score * 0.10
                + fit_score * 0.10
                - repetition_penalty
            )
        else:
            total = utility_score * 0.6 + freshness_score * 0.4 + type_boost - repetition_penalty

        reason = {
            OutfitType.SAFE.value: "经典颜色搭配，适合日常通勤",
            OutfitType.FAST.value: "你最近常穿的单品，省时间",
            OutfitType.STYLISH.value: f"换个风格试试：{combo[0].title}",
        }[outfit_type]
        title = {
            OutfitType.SAFE.value: "最稳妥",
            OutfitType.FAST.value: "最省时间",
            OutfitType.STYLISH.value: "更有风格感",
        }[outfit_type]
        return EngineOutfit(
            outfit_type=outfit_type,
            title=title,
            reason_text=reason,
            item_ids=[item.item_id for item in combo],
            score_total=round(max(0.0, total), 4),
            score_breakdown={
                "utility_score": utility_score,
                "color_score": utility_score,
                "freshness_score": freshness_score,
                "aesthetic_score": aesthetic_score,
                "time_cost_score": time_cost_score,
                "weather_score": weather_score,
                "fit_score": fit_score,
                "repetition_penalty": repetition_penalty,
            },
        )

    def _time_cost_score(self, combo: list[EngineItem]) -> float:
        locations = [item.location for item in combo if item.location]
        if locations:
            max_same_location = max(locations.count(location) for location in set(locations))
            score = 0.5 + 0.5 * (max_same_location / len(combo))
        else:
            score = 0.5
        if any(item.fit_status in FIT_DOWNGRADE_STATUSES for item in combo):
            score -= 0.15
        return _clamp_score(score)

    def _weather_score(self, combo: list[EngineItem], weather: WeatherInfo | None) -> float:
        if weather is None:
            return 0.5
        score = 0.8
        categories = {item.category for item in combo}
        if weather.temperature_c < 10 and Category.OUTERWEAR.value not in categories:
            score -= 0.35
        if weather.rain_probability > 0.5 and any(
            item.category == Category.SHOES.value and item.color_main.lower() in LIGHT_SHOE_COLORS
            for item in combo
        ):
            score -= 0.25
        return _clamp_score(score)

    def _fit_score(self, combo: list[EngineItem]) -> float:
        scores = []
        for item in combo:
            score = 0.5
            if item.fit_status == "fits_well":
                score += 0.1
            elif item.fit_status in FIT_DOWNGRADE_STATUSES:
                score -= 0.15
            scores.append(score)
        return _clamp_score(sum(scores) / len(scores)) if scores else 0.5

    def _repetition_penalty(
        self, combo: list[EngineItem], recent_worn_outfit_item_ids: list[set[str]]
    ) -> float:
        current_item_ids = {item.item_id for item in combo}
        for worn_item_ids in recent_worn_outfit_item_ids:
            union = current_item_ids | worn_item_ids
            if not union:
                continue
            similarity = len(current_item_ids & worn_item_ids) / len(union)
            if similarity > REPETITION_SIMILARITY_THRESHOLD:
                return REPETITION_PENALTY
        return 0.0

    def _main_item(self, item_ids: list[str], items: list[EngineItem]) -> str | None:
        item_by_id = {item.item_id: item for item in items}
        for item_id in item_ids:
            item = item_by_id[item_id]
            if item.category in {Category.TOP.value, Category.DRESS.value}:
                return item_id
        return item_ids[0] if item_ids else None


def _clamp_score(value: float) -> float:
    return min(1.0, max(0.0, value))
