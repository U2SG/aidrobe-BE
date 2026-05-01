from dataclasses import dataclass
from datetime import date

from app.application.recommendations.colors import color_score
from app.common.enums import Category, OutfitType


@dataclass(frozen=True)
class EngineItem:
    item_id: str
    title: str
    category: str
    color_main: str


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
    ) -> EngineResult:
        recent_worn_main_item_ids = recent_worn_main_item_ids or set()
        wearable = items
        if len(wearable) < 3:
            return EngineResult(
                status="insufficient_items",
                outfits=[],
                guidance_text="再加一件下装就能搭",
            )

        candidates = self._build_candidates(wearable, recent_worn_main_item_ids)
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
        self, items: list[EngineItem], recent_worn_main_item_ids: set[str]
    ) -> list[EngineOutfit]:
        tops = [item for item in items if item.category == Category.TOP.value]
        bottoms = [item for item in items if item.category == Category.BOTTOM.value]
        dresses = [item for item in items if item.category == Category.DRESS.value]
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

        if shoes:
            combos = [
                combo + [shoes[0]]
                if all(i.category != Category.SHOES.value for i in combo)
                else combo
                for combo in combos
            ]

        for combo in combos:
            for outfit_type in [
                OutfitType.SAFE.value,
                OutfitType.FAST.value,
                OutfitType.STYLISH.value,
            ]:
                candidates.append(self._score_combo(combo, outfit_type, recent_worn_main_item_ids))
        return candidates

    def _score_combo(
        self, combo: list[EngineItem], outfit_type: str, recent_worn_main_item_ids: set[str]
    ) -> EngineOutfit:
        colors = [item.color_main for item in combo]
        c_score = color_score(colors)
        main_item_id = self._main_item([item.item_id for item in combo], combo)
        freshness_score = 0.3 if main_item_id in recent_worn_main_item_ids else 1.0
        type_boost = {
            OutfitType.SAFE.value: 0.05,
            OutfitType.FAST.value: 0.03,
            OutfitType.STYLISH.value: 0.01,
        }[outfit_type]
        total = c_score * 0.6 + freshness_score * 0.4 + type_boost
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
            score_total=round(total, 4),
            score_breakdown={"color_score": c_score, "freshness_score": freshness_score},
        )

    def _main_item(self, item_ids: list[str], items: list[EngineItem]) -> str | None:
        item_by_id = {item.item_id: item for item in items}
        for item_id in item_ids:
            item = item_by_id[item_id]
            if item.category in {Category.TOP.value, Category.DRESS.value}:
                return item_id
        return item_ids[0] if item_ids else None
