from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.application.ports.llm import LlmPort
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

_REASON_TEMPLATES: dict[str, str] = {
    OutfitType.SAFE.value: "经典颜色搭配，适合日常通勤",
    OutfitType.FAST.value: "你最近常穿的单品，省时间",
    OutfitType.STYLISH.value: "换个风格试试：{title}",
}

_TITLE_TEMPLATES: dict[str, str] = {
    OutfitType.SAFE.value: "最稳妥",
    OutfitType.FAST.value: "最省时间",
    OutfitType.STYLISH.value: "更有风格感",
}


@dataclass(frozen=True)
class EngineItem:
    item_id: str
    title: str
    category: str
    color_main: str
    location: str | None = None
    fit_status: str = "unknown"
    fit_intent: str | None = None


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
    def __init__(self, llm: LlmPort | None = None) -> None:
        self.llm = llm

    async def generate(
        self,
        items: list[EngineItem],
        recent_worn_main_item_ids: set[str] | None = None,
        target_date: date | None = None,
        style_profile: dict[str, Any] | None = None,
        weather: WeatherInfo | None = None,
        recent_worn_outfit_item_ids: list[set[str]] | None = None,
        fit_profile: dict[str, Any] | None = None,
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

        candidates = await self._build_candidates(
            wearable,
            recent_worn_main_item_ids,
            style_profile or {},
            weather,
            recent_worn_outfit_item_ids,
            fit_profile or {},
        )
        if not candidates:
            return EngineResult(
                status="insufficient_items",
                outfits=[],
                guidance_text="再加一件下装就能搭",
            )

        # Fill one outfit per type (safe / fast / stylish) via a de-dup *ladder*,
        # not a hard distinct-main-item rule. The old rule reserved each top for a
        # single variant, so a small wardrobe (or one dominant top) silently
        # collapsed to a single outfit. Level 1 still prefers a distinct main item
        # (most varied); level 2 fills the leftover slots allowing the same top in
        # a *different core pairing* (same shirt, different trousers). We stop at
        # the core combo — two variants never differ by shoes/accessory alone.
        ordered_types = [OutfitType.SAFE.value, OutfitType.FAST.value, OutfitType.STYLISH.value]
        chosen: dict[str, EngineOutfit] = {}
        used_main: set[str] = set()
        used_core: set[tuple[str, ...]] = set()

        def _fill(key_fn: Callable[[EngineOutfit], Any], blocked: set[Any]) -> None:
            for outfit_type in ordered_types:
                if outfit_type in chosen:
                    continue
                pool = [
                    candidate
                    for candidate in candidates
                    if candidate.outfit_type == outfit_type and key_fn(candidate) not in blocked
                ]
                if not pool:
                    continue
                best = max(pool, key=lambda candidate: candidate.score_total)
                chosen[outfit_type] = best
                main_item = self._main_item(best.item_ids, wearable)
                if main_item:
                    used_main.add(main_item)
                used_core.add(self._core_key(best.item_ids, wearable))

        _fill(lambda candidate: self._main_item(candidate.item_ids, wearable), used_main)
        _fill(lambda candidate: self._core_key(candidate.item_ids, wearable), used_core)

        selected = [chosen[outfit_type] for outfit_type in ordered_types if outfit_type in chosen]
        if not selected and candidates:
            selected.append(max(candidates, key=lambda candidate: candidate.score_total))

        return EngineResult(status="ready", outfits=selected[:3])

    async def _build_candidates(
        self,
        items: list[EngineItem],
        recent_worn_main_item_ids: set[str],
        style_profile: dict[str, Any],
        weather: WeatherInfo | None,
        recent_worn_outfit_item_ids: list[set[str]],
        fit_profile: dict[str, Any],
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
                    await self._score_combo(
                        combo,
                        outfit_type,
                        recent_worn_main_item_ids,
                        style_profile,
                        weather,
                        recent_worn_outfit_item_ids,
                        fit_profile,
                    )
                )
        return candidates

    async def _score_combo(
        self,
        combo: list[EngineItem],
        outfit_type: str,
        recent_worn_main_item_ids: set[str],
        style_profile: dict[str, Any],
        weather: WeatherInfo | None,
        recent_worn_outfit_item_ids: list[set[str]],
        fit_profile: dict[str, Any],
    ) -> EngineOutfit:
        colors = [item.color_main for item in combo]
        categories = [item.category for item in combo]
        utility_score = color_score(colors)
        main_item_id = self._main_item([item.item_id for item in combo], combo)
        freshness_score = 0.3 if main_item_id in recent_worn_main_item_ids else 1.0
        aesthetic_score = compute_aesthetic_score(colors, categories, style_profile)
        time_cost_score = self._time_cost_score(combo)
        weather_score = self._weather_score(combo, weather)
        fit_score = self._fit_score(combo, fit_profile)
        repetition_penalty = self._repetition_penalty(combo, recent_worn_outfit_item_ids)
        type_boost = {
            OutfitType.SAFE.value: 0.05,
            OutfitType.FAST.value: 0.03,
            OutfitType.STYLISH.value: 0.01,
        }[outfit_type]

        has_profile = bool(style_profile.get("color_weights") or style_profile.get("category_weights"))
        has_fit_signal = bool(fit_profile) or any(item.fit_status != "unknown" for item in combo)
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
        elif has_fit_signal:
            total = (
                utility_score * 0.50
                + freshness_score * 0.30
                + fit_score * 0.20
                + type_boost
                - repetition_penalty
            )
        else:
            total = utility_score * 0.6 + freshness_score * 0.4 + type_boost - repetition_penalty

        title = _TITLE_TEMPLATES[outfit_type]
        template = _REASON_TEMPLATES[outfit_type]
        reason = template.format(title=combo[0].title)

        # Polish reason with LLM if available
        if self.llm is not None:
            try:
                prompt = self._build_reason_prompt(outfit_type, combo, colors, categories)
                polished = await self.llm.polish_reason(prompt)
                if polished.strip():
                    reason = polished.strip()
            except Exception:
                # Fall through to template reason
                pass

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

    def _build_reason_prompt(
        self,
        outfit_type: str,
        combo: list[EngineItem],
        colors: list[str],
        categories: list[str],
    ) -> str:
        items_desc = "、".join(
            f"{item.category}（{item.color_main}）" for item in combo
        )
        type_labels = {
            OutfitType.SAFE.value: "稳妥通勤",
            OutfitType.FAST.value: "省时快捷",
            OutfitType.STYLISH.value: "风格尝试",
        }
        return (
            f"你是一个穿搭助手。请为一个推荐结果写一条简短的理由，15字以内，中文。\n"
            f"搭配风格：{type_labels.get(outfit_type, outfit_type)}\n"
            f"包含单品：{items_desc}\n"
            f"颜色组合：{' + '.join(colors)}\n"
            f"品类组合：{' + '.join(categories)}\n\n"
            f"写出自然、有说服力的推荐理由，不要重复\"经典颜色搭配\"等套话。"
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

    def _fit_score(self, combo: list[EngineItem], fit_profile: dict[str, Any]) -> float:
        """Score fit quality per item, adjusted by learned user preferences.

        fit_profile may be either the persisted profile shape or a legacy
        category → preference mapping:
          "prefers_loose" → boosts items with loose/oversized signals
          "prefers_fitted" → boosts items with fitted/tight signals
          "balanced" → neutral
        """
        category_preferences = _category_fit_preferences(fit_profile)
        body_shape_weights = _body_shape_tag_weights(fit_profile)
        scores = []
        for item in combo:
            score = 0.5
            if item.fit_status == "fits_well":
                score += 0.1
            elif item.fit_status in FIT_DOWNGRADE_STATUSES:
                score -= 0.15

            # Cross-item preference adjustment from learned fit profile
            pref = category_preferences.get(item.category)
            if pref == "prefers_loose":
                if item.fit_status in ("loose", "oversized_ok"):
                    score += 0.1
                elif item.fit_status == "tight":
                    score -= 0.1
            elif pref == "prefers_fitted":
                if item.fit_status == "tight":
                    score += 0.05
                elif item.fit_status in ("loose", "oversized_ok"):
                    score -= 0.1

            if item.fit_intent:
                score += _fit_weight(body_shape_weights.get(item.fit_intent)) * 0.08
                score += _fit_weight(body_shape_weights.get(f"{item.category}:{item.fit_intent}")) * 0.12

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

    def _core_key(self, item_ids: list[str], items: list[EngineItem]) -> tuple[str, ...]:
        """Identity of an outfit's *core* (top + bottom, or a dress) — the pieces
        that decide whether two variants are genuinely different looks. Shoes,
        outerwear and accessories are deliberately excluded so that swapping only
        a bag/shoe does not count as a new outfit."""
        item_by_id = {item.item_id: item for item in items}
        core_categories = {Category.TOP.value, Category.BOTTOM.value, Category.DRESS.value}
        return tuple(
            sorted(
                item_id
                for item_id in item_ids
                if item_by_id[item_id].category in core_categories
            )
        )


def _clamp_score(value: float) -> float:
    return min(1.0, max(0.0, value))


def _category_fit_preferences(fit_profile: dict[str, Any]) -> dict[str, str]:
    preferences = fit_profile.get("category_preferences")
    if isinstance(preferences, dict):
        return {
            str(category): str(preference)
            for category, preference in preferences.items()
            if isinstance(preference, str)
        }
    return {
        str(category): str(preference)
        for category, preference in fit_profile.items()
        if isinstance(preference, str)
    }


def _body_shape_tag_weights(fit_profile: dict[str, Any]) -> dict[str, float]:
    weights = fit_profile.get("body_shape_tag_weights")
    if not isinstance(weights, dict):
        return {}
    return {
        str(tag): _fit_weight(weight)
        for tag, weight in weights.items()
        if isinstance(weight, int | float)
    }


def _fit_weight(value: Any) -> float:
    if not isinstance(value, int | float):
        return 0.0
    return min(1.0, max(-1.0, float(value)))
