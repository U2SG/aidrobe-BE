from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date
from itertools import combinations
from typing import Any

from app.application.llm_prompts.persona import stylist_persona
from app.application.ports.llm import LlmPort
from app.application.ports.weather import WeatherInfo
from app.application.recommendations.colors import color_score
from app.application.recommendations.indoor_climate import IndoorClimateSignal
from app.application.recommendations.reasoning import build_reasoning
from app.application.safety.copy_guard import sanitize_generated_copy
from app.application.style.build_style_profile import aesthetic_score as compute_aesthetic_score
from app.common.enums import Category, OutfitType
from app.infrastructure.llm.sanitizer import sanitize_llm_text
from app.infrastructure.persistence.models.wardrobe_relationship import (
    POSITIVE_RELATIONSHIP_TYPES,
    REJECTED_RELATIONSHIP_TYPES,
)

# BE#66 — short cap for any value that enters the reason-polish prompt. These are
# enum/derived strings today, but bounding them keeps the "no unbounded user text
# in a prompt" invariant if a free-text field is ever added to the combo.
_PROMPT_VALUE_MAX_LEN = 40


def _clip(value: str) -> str:
    return sanitize_llm_text(value, max_len=_PROMPT_VALUE_MAX_LEN)

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
# BE#67 — feedback suppression: an outfit whose item-set closely matches one the
# user recently dismissed/disliked is down-ranked. Stronger than the repetition
# penalty (explicit negative feedback > "you wore this lately"), but a bounded
# soft penalty — never a hard exclude — so it stays reversible.
SUPPRESSION_SIMILARITY_THRESHOLD = REPETITION_SIMILARITY_THRESHOLD
SUPPRESSION_PENALTY = 0.5
# BE#46 — per-item penalty applied when an item's color matches an explicit
# user-stated NEGATIVE color preference. Multiplied by how many items in the
# combo match, so an outfit built mostly of disliked colors is pushed well below
# alternatives without being hard-excluded (the user might still own only those).
DISLIKED_COLOR_PENALTY = 0.3
WARDROBE_GRAPH_PAIR_BOOST = 0.18
WARDROBE_GRAPH_REJECTION_PENALTY = 0.8

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
class RelationshipEdge:
    source_item_id: str
    target_item_id: str
    relationship_type: str
    score: float
    confidence: float


@dataclass(frozen=True)
class EngineOutfit:
    outfit_type: str
    title: str
    reason_text: str
    item_ids: list[str]
    score_total: float
    score_breakdown: dict[str, float]
    # BE#38/#85 — grounded {type, text, source, referenced_item_ids, confidence}
    # reasons; populated for the selected outfits.
    reasoning: list[dict[str, Any]] = field(default_factory=list)


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
        avoid_exact_repeats: bool = False,
        disliked_colors: set[str] | None = None,
        relationship_edges: list[RelationshipEdge] | None = None,
        suppressed_outfit_item_ids: list[set[str]] | None = None,
        indoor_climate: IndoorClimateSignal | None = None,
    ) -> EngineResult:
        recent_worn_main_item_ids = recent_worn_main_item_ids or set()
        recent_worn_outfit_item_ids = recent_worn_outfit_item_ids or []
        disliked_colors = disliked_colors or set()
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
            disliked_colors,
            indoor_climate,
        )
        if not candidates:
            return EngineResult(
                status="insufficient_items",
                outfits=[],
                guidance_text="再加一件下装就能搭",
            )
        candidates = self._apply_relationship_ranking(
            candidates,
            relationship_edges or [],
        )
        candidates = self._apply_suppression(
            candidates,
            suppressed_outfit_item_ids or [],
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
        # BE#39 — hard-exclude outfits whose EXACT item-set repeats one seen in the
        # recent window. The caller merges worn + recently-recommended sets into
        # `recent_worn_outfit_item_ids`, so "the exact same look again" is blocked
        # (the soft Jaccard penalty still down-ranks merely-similar outfits). Gated
        # by `avoid_exact_repeats` so flag-off behavior is unchanged.
        recent_sets = {frozenset(s) for s in recent_worn_outfit_item_ids}

        def _fill(
            key_fn: Callable[[EngineOutfit], Any],
            blocked: set[Any],
            *,
            exclude_recent: bool,
        ) -> None:
            for outfit_type in ordered_types:
                if outfit_type in chosen:
                    continue
                pool = [
                    candidate
                    for candidate in candidates
                    if candidate.outfit_type == outfit_type
                    and key_fn(candidate) not in blocked
                    and not (exclude_recent and frozenset(candidate.item_ids) in recent_sets)
                ]
                if not pool:
                    continue
                best = max(pool, key=lambda candidate: candidate.score_total)
                chosen[outfit_type] = best
                main_item = self._main_item(best.item_ids, wearable)
                if main_item:
                    used_main.add(main_item)
                used_core.add(self._core_key(best.item_ids, wearable))

        _fill(
            lambda candidate: self._main_item(candidate.item_ids, wearable),
            used_main,
            exclude_recent=avoid_exact_repeats,
        )
        _fill(
            lambda candidate: self._core_key(candidate.item_ids, wearable),
            used_core,
            exclude_recent=avoid_exact_repeats,
        )
        if avoid_exact_repeats:
            # Small-wardrobe fallback: if excluding exact recent repeats left a
            # type unfilled, relax the exclusion so we still return outfits
            # rather than fewer than the wardrobe could otherwise support.
            _fill(
                lambda candidate: self._core_key(candidate.item_ids, wearable),
                used_core,
                exclude_recent=False,
            )

        selected = [chosen[outfit_type] for outfit_type in ordered_types if outfit_type in chosen]
        if not selected and candidates:
            selected.append(max(candidates, key=lambda candidate: candidate.score_total))
        selected = selected[:3]

        # BE#38 — attach grounded reasoning to the chosen outfits only (cheap:
        # ≤3, not per-candidate). Grounded in the same signals scoring used.
        item_by_id = {item.item_id: item for item in wearable}
        preferred_colors = set((style_profile or {}).get("color_weights", {}))
        selected = [
            replace(
                outfit,
                reasoning=build_reasoning(
                    categories=[item_by_id[i].category for i in outfit.item_ids if i in item_by_id],
                    colors=[item_by_id[i].color_main for i in outfit.item_ids if i in item_by_id],
                    fit_statuses=[
                        item_by_id[i].fit_status for i in outfit.item_ids if i in item_by_id
                    ],
                    is_fresh=self._main_item(outfit.item_ids, wearable)
                    not in recent_worn_main_item_ids,
                    weather=weather,
                    preferred_colors=preferred_colors,
                    # BE#85 — pass item ids (parallel to categories/colors/fit_statuses)
                    # and the freshness-relevant main item so provenance can attach
                    # specific UUIDs to each reason entry.
                    item_ids=[i for i in outfit.item_ids if i in item_by_id],
                    main_item_id=self._main_item(outfit.item_ids, wearable),
                    indoor_climate=indoor_climate.tendency if indoor_climate else None,
                ),
            )
            for outfit in selected
        ]

        return EngineResult(status="ready", outfits=selected)

    async def _build_candidates(
        self,
        items: list[EngineItem],
        recent_worn_main_item_ids: set[str],
        style_profile: dict[str, Any],
        weather: WeatherInfo | None,
        recent_worn_outfit_item_ids: list[set[str]],
        fit_profile: dict[str, Any],
        disliked_colors: set[str],
        indoor_climate: IndoorClimateSignal | None = None,
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
                        disliked_colors,
                        indoor_climate,
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
        disliked_colors: set[str],
        indoor_climate: IndoorClimateSignal | None = None,
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
        # BE#46 — explicit negative color preference: one penalty unit per item
        # whose color the user dislikes. Empty set (flag off) → 0.0, legacy.
        disliked_penalty = self._disliked_color_penalty(combo, disliked_colors)
        # BE#82 — indoor-climate nudge: prefer a removable outer layer when a
        # tendency was inferred. None (flag off) → 0.0, byte-identical to legacy.
        indoor_climate_bonus = self._indoor_climate_score(combo, indoor_climate)
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
                - disliked_penalty
                + indoor_climate_bonus
            )
        elif has_fit_signal:
            total = (
                utility_score * 0.50
                + freshness_score * 0.30
                + fit_score * 0.20
                + type_boost
                - repetition_penalty
                - disliked_penalty
                + indoor_climate_bonus
            )
        else:
            total = (
                utility_score * 0.6
                + freshness_score * 0.4
                + type_boost
                - repetition_penalty
                - disliked_penalty
                + indoor_climate_bonus
            )

        title = _TITLE_TEMPLATES[outfit_type]
        template = _REASON_TEMPLATES[outfit_type]
        reason = template.format(title=combo[0].title)

        # Polish reason with LLM if available
        if self.llm is not None:
            try:
                prompt = self._build_reason_prompt(
                    outfit_type,
                    combo,
                    colors,
                    categories,
                    weather=weather,
                    is_fresh=main_item_id not in recent_worn_main_item_ids,
                )
                polished = await self.llm.polish_reason(prompt)
                if polished.strip():
                    reason = sanitize_generated_copy(
                        polished.strip(), fallback=reason
                    )
            except Exception:
                # Fall through to template reason
                pass

        score_breakdown = {
            "utility_score": utility_score,
            "color_score": utility_score,
            "freshness_score": freshness_score,
            "aesthetic_score": aesthetic_score,
            "time_cost_score": time_cost_score,
            "weather_score": weather_score,
            "fit_score": fit_score,
            "repetition_penalty": repetition_penalty,
            "disliked_color_penalty": disliked_penalty,
        }
        # BE#82 — only surface the key when the feature is actually applied, so a
        # flag-off breakdown keeps its exact legacy shape.
        if indoor_climate is not None:
            score_breakdown["indoor_climate_bonus"] = indoor_climate_bonus
        return EngineOutfit(
            outfit_type=outfit_type,
            title=title,
            reason_text=reason,
            item_ids=[item.item_id for item in combo],
            score_total=round(max(0.0, total), 4),
            score_breakdown=score_breakdown,
        )

    def _build_reason_prompt(
        self,
        outfit_type: str,
        combo: list[EngineItem],
        colors: list[str],
        categories: list[str],
        *,
        weather: WeatherInfo | None = None,
        is_fresh: bool = False,
    ) -> str:
        # BE#66 — bound every value that reaches the prompt (closes the prior
        # sanitizer bypass on raw weather.condition / item fields) and lead with
        # the shared stylist persona so Today speaks the same voice as Ask.
        items_desc = "、".join(
            f"{_clip(item.category)}（{_clip(item.color_main)}）" for item in combo
        )
        type_labels = {
            OutfitType.SAFE.value: "稳妥通勤",
            OutfitType.FAST.value: "省时快捷",
            OutfitType.STYLISH.value: "风格尝试",
        }
        # BE#37 — ground the copy in real context (weather + wear recency) so it
        # reads as personalized for today, not a generic template. Only facts
        # that are actually true are added, so the model can't over-claim.
        context_lines = ""
        if weather is not None:
            context_lines += f"今日天气：{_clip(weather.condition)}，约 {round(weather.temperature_c)}°C"
            if weather.rain_probability > 0.5:
                context_lines += "，可能有雨"
            context_lines += "\n"
        if is_fresh:
            context_lines += "这套的主要单品用户最近没穿过\n"
        colors_desc = " + ".join(_clip(c) for c in colors)
        categories_desc = " + ".join(_clip(c) for c in categories)
        return (
            f"{stylist_persona('zh')}\n"
            f"请为一个推荐结果写一条简短的理由，15字以内，中文。\n"
            f"搭配风格：{type_labels.get(outfit_type, outfit_type)}\n"
            f"包含单品：{items_desc}\n"
            f"颜色组合：{colors_desc}\n"
            f"品类组合：{categories_desc}\n"
            f"{context_lines}\n"
            f"结合今日天气与穿着情况，写出自然、有说服力的推荐理由，"
            f"不要重复\"经典颜色搭配\"等套话，不要编造未提供的信息。"
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

    def _indoor_climate_score(
        self, combo: list[EngineItem], indoor_climate: IndoorClimateSignal | None
    ) -> float:
        """BE#82 — small additive nudge toward a removable outer layer when an
        indoor-climate tendency was inferred: an outer layer can be added when the
        office AC runs cold, or shed when it runs warm, closing the indoor/outdoor
        gap. None (flag off / no inference) → 0.0, so ranking stays legacy."""
        if indoor_climate is None:
            return 0.0
        has_outerwear = any(item.category == Category.OUTERWEAR.value for item in combo)
        return 0.08 if has_outerwear else 0.0

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

    def _disliked_color_penalty(
        self, combo: list[EngineItem], disliked_colors: set[str]
    ) -> float:
        """One `DISLIKED_COLOR_PENALTY` unit per combo item whose color the user
        explicitly dislikes. 0.0 when the set is empty (flag off)."""
        if not disliked_colors:
            return 0.0
        hits = sum(
            1 for item in combo if item.color_main and item.color_main.lower() in disliked_colors
        )
        return round(hits * DISLIKED_COLOR_PENALTY, 4)

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

    def _apply_relationship_ranking(
        self,
        candidates: list[EngineOutfit],
        relationship_edges: list[RelationshipEdge],
    ) -> list[EngineOutfit]:
        if not relationship_edges:
            return candidates
        relationship_index = _relationship_index(relationship_edges)
        adjusted: list[EngineOutfit] = []
        for candidate in candidates:
            boost, penalty = _relationship_adjustment(candidate.item_ids, relationship_index)
            if boost == 0.0 and penalty == 0.0:
                adjusted.append(candidate)
                continue
            score_breakdown = dict(candidate.score_breakdown)
            score_breakdown["wardrobe_graph_boost"] = round(boost, 4)
            score_breakdown["wardrobe_graph_penalty"] = round(penalty, 4)
            adjusted.append(
                replace(
                    candidate,
                    score_total=round(max(0.0, candidate.score_total + boost - penalty), 4),
                    score_breakdown=score_breakdown,
                )
            )
        return adjusted

    def _apply_suppression(
        self,
        candidates: list[EngineOutfit],
        suppressed_outfit_item_ids: list[set[str]],
    ) -> list[EngineOutfit]:
        """BE#67 — down-rank candidates whose item-set closely matches a recently
        dismissed/disliked outfit (Jaccard ≥ threshold). Mirrors the relationship
        post-scoring step; records the penalty in score_breakdown for auditability."""
        if not suppressed_outfit_item_ids:
            return candidates
        suppressed_sets = [set(s) for s in suppressed_outfit_item_ids if s]
        if not suppressed_sets:
            return candidates
        adjusted: list[EngineOutfit] = []
        for candidate in candidates:
            current = set(candidate.item_ids)
            penalize = False
            for disliked in suppressed_sets:
                union = current | disliked
                if union and len(current & disliked) / len(union) >= SUPPRESSION_SIMILARITY_THRESHOLD:
                    penalize = True
                    break
            if not penalize:
                adjusted.append(candidate)
                continue
            score_breakdown = dict(candidate.score_breakdown)
            score_breakdown["feedback_suppression_penalty"] = SUPPRESSION_PENALTY
            adjusted.append(
                replace(
                    candidate,
                    score_total=round(max(0.0, candidate.score_total - SUPPRESSION_PENALTY), 4),
                    score_breakdown=score_breakdown,
                )
            )
        return adjusted

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


def _relationship_index(
    relationship_edges: list[RelationshipEdge],
) -> dict[frozenset[str], list[RelationshipEdge]]:
    indexed: dict[frozenset[str], list[RelationshipEdge]] = {}
    for edge in relationship_edges:
        key = frozenset((edge.source_item_id, edge.target_item_id))
        indexed.setdefault(key, []).append(edge)
    return indexed


def _relationship_adjustment(
    item_ids: list[str],
    relationship_index: dict[frozenset[str], list[RelationshipEdge]],
) -> tuple[float, float]:
    boost = 0.0
    penalty = 0.0
    for left, right in combinations(item_ids, 2):
        for edge in relationship_index.get(frozenset((left, right)), []):
            confidence = _clamp_score(edge.confidence)
            if edge.relationship_type in REJECTED_RELATIONSHIP_TYPES or edge.score < -0.3:
                penalty += max(abs(edge.score), 0.5) * confidence * WARDROBE_GRAPH_REJECTION_PENALTY
            elif edge.relationship_type in POSITIVE_RELATIONSHIP_TYPES and edge.score > 0:
                boost += edge.score * confidence * WARDROBE_GRAPH_PAIR_BOOST
    return min(0.3, boost), min(1.0, penalty)


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
