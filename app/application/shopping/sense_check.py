"""M17 — Shopping Sense-Check use case (rule + embedding, no LLM).

Pipeline:
    1. Vision identifies the candidate item's attributes (category, color).
    2. Embedding-similarity match against the user's wardrobe. When pgvector
       is enabled we rank by candidate-image vector vs stored item vectors;
       otherwise we fall back to the category/color heuristic the rest of the
       codebase already uses (see `outfits/replacements.py`). The fallback is
       the path CI exercises (PGVECTOR_ENABLED unset).
    3. Assess duplication & gap by category/color, then derive a verdict.

`outfit_potential` = how many wearable items in complementary categories the
candidate could pair with (capped). `wardrobe_gap` = `Category` values for
which the user owns zero wearable items. The exact weights are a judgement
call; the requirement is determinism under the fake vision provider.
"""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import Float, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports.vision import VisionPort, VisionResult
from app.application.preferences.memory import live_negative_values_by_type
from app.common import error_codes
from app.common.enums import Category, ItemStatus
from app.common.exceptions import FriendlyException
from app.common.i18n import I18nMessage, make_message
from app.common.item_labels import localized_item_name
from app.common.locale import resolve_locale
from app.config.settings import get_settings
from app.infrastructure.importers.link_parser import _is_safe_url
from app.infrastructure.persistence.models.embedding import ItemEmbedding, is_current_embedding
from app.infrastructure.persistence.models.shopping import ShoppingSenseCheck
from app.infrastructure.persistence.models.user import UserSettings
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.wardrobe_repo import load_wardrobe_image_sources
from app.infrastructure.storage.source_image import InvalidSourceImage, resolve_import_source_key

# Similarity scores assigned by the deterministic fallback path. Real cosine
# scores (pgvector branch) override these when available.
SIM_CATEGORY_COLOR = 0.9
_SIM_CATEGORY_ONLY = 0.6

# Complementary categories used to estimate outfit_potential. A top pairs with
# bottoms/shoes/outerwear; a dress is largely standalone. Kept intentionally
# coarse — VisionResult only yields category + color.
_COMPLEMENTS: dict[str, tuple[str, ...]] = {
    Category.TOP.value: (Category.BOTTOM.value, Category.SHOES.value, Category.OUTERWEAR.value),
    Category.BOTTOM.value: (Category.TOP.value, Category.SHOES.value, Category.OUTERWEAR.value),
    Category.OUTERWEAR.value: (Category.TOP.value, Category.BOTTOM.value, Category.SHOES.value),
    Category.DRESS.value: (Category.SHOES.value, Category.OUTERWEAR.value),
    Category.SHOES.value: (Category.TOP.value, Category.BOTTOM.value, Category.DRESS.value),
}
_OUTFIT_POTENTIAL_CAP = 10

# BE#69 — redundancy level thresholds on the top similar-item score.
_REDUNDANCY_HIGH = SIM_CATEGORY_COLOR  # 0.9 — same category + color
_REDUNDANCY_MEDIUM = 0.7
# Core categories whose absence is a high-severity wardrobe gap.
_CORE_CATEGORIES = {
    Category.TOP.value,
    Category.BOTTOM.value,
    Category.SHOES.value,
    Category.OUTERWEAR.value,
}
_MAX_OUTFIT_SAMPLES = 2


def _pgvector_enabled() -> bool:
    return os.getenv("PGVECTOR_ENABLED", "false").lower() in ("1", "true", "yes")


@dataclass
class SimilarItem:
    item_id: uuid.UUID
    similarity: float
    name: str
    category: str
    color_main: str
    thumbnail_object_key: str | None = None
    thumbnail_fallback_url: str | None = None


@dataclass
class SenseCheckResult:
    verdict: str  # buy / consider / skip
    reason: str
    display_reason: I18nMessage
    similar_item_count: int = 0
    similar_items: list[SimilarItem] = field(default_factory=list)
    wardrobe_gap: list[str] = field(default_factory=list)
    outfit_potential: int = 0
    # Identified candidate attributes — echoed back and snapshotted in payload.
    category: str = ""
    color_main: str = ""
    # BE#69 — wardrobe-aware evaluation block (None unless ENABLE_PURCHASE_EVALUATOR):
    # {confidence, redundancy, outfit_samples, wardrobe_gap_detail, factors,
    # cost_per_wear_hint}. Persisted in payload + surfaced on the response.
    evaluation: dict[str, Any] | None = None


class SenseCheck:
    """Evaluate a candidate purchase against the user's wardrobe."""

    def __init__(self, session: AsyncSession, vision: VisionPort) -> None:
        self.session = session
        self.vision = vision

    async def execute(
        self,
        user_id: uuid.UUID,
        *,
        image_url: str | None = None,
        source_url: str | None = None,
        locale: str | None = None,
        price: float | None = None,
    ) -> tuple[SenseCheckResult, ShoppingSenseCheck]:
        if image_url is None and source_url is None:
            raise FriendlyException(
                error_codes.VALIDATION_ERROR,
                "image_url or source_url is required",
                status_code=422,
            )
        source_key = None
        if image_url is not None:
            try:
                source_key = resolve_import_source_key(image_url, user_id, get_settings())
            except InvalidSourceImage as exc:
                raise FriendlyException(error_codes.VALIDATION_ERROR, "Unsafe URL", status_code=400) from exc
            if source_key is None and not _is_safe_url(image_url):
                raise FriendlyException(error_codes.VALIDATION_ERROR, "Unsafe URL", status_code=400)
            analyze_target: str = image_url
        else:
            assert source_url is not None
            if not _is_safe_url(source_url):
                raise FriendlyException(error_codes.VALIDATION_ERROR, "Unsafe URL", status_code=400)
            from app.infrastructure.importers.router import RoutedLinkParser

            parser = RoutedLinkParser(get_settings())
            extracted = await parser.extract_image_url(source_url)
            if not extracted:
                raise FriendlyException(
                    error_codes.VALIDATION_ERROR,
                    "Cannot extract image from URL",
                    status_code=422,
                )
            analyze_target = extracted
        try:
            vision_result = await self.vision.analyze_image(analyze_target, source_key=source_key)
        except (ValueError, RuntimeError, httpx.HTTPError, TimeoutError) as exc:
            raise FriendlyException(
                error_codes.VALIDATION_ERROR,
                "Could not identify a clothing item from the input",
                status_code=422,
            ) from exc

        settings_locale = await self.session.scalar(
            select(UserSettings.locale).where(UserSettings.user_id == user_id)
        )
        effective_locale = resolve_locale(locale, settings_locale)
        similar = await self._find_similar(user_id, vision_result, effective_locale)
        gap = await self._wardrobe_gap(user_id)
        outfit_potential = await self._outfit_potential(user_id, vision_result)
        verdict, display_reason, similar_item_count = self._decide(
            vision_result, similar, gap, outfit_potential
        )

        evaluation: dict[str, Any] | None = None
        if get_settings().ENABLE_PURCHASE_EVALUATOR:
            verdict, display_reason, evaluation = await self._evaluate(
                user_id,
                vision_result,
                similar=similar,
                gap=gap,
                outfit_potential=outfit_potential,
                verdict=verdict,
                display_reason=display_reason,
                price=price,
            )

        result = SenseCheckResult(
            verdict=verdict,
            reason=display_reason.fallback_text,
            display_reason=display_reason,
            similar_item_count=similar_item_count,
            similar_items=similar,
            wardrobe_gap=gap,
            outfit_potential=outfit_potential,
            category=vision_result.category,
            color_main=vision_result.color_main,
            evaluation=evaluation,
        )
        record = await self._persist(user_id, image_url if source_key is None else None, source_url, result, source_key)
        return result, record

    async def _find_similar(
        self, user_id: uuid.UUID, candidate: VisionResult, locale: str
    ) -> list[SimilarItem]:
        """Up to K wardrobe items most similar to the candidate.

        Embedding branch only when pgvector is on (mirrors replacements.py —
        the JSONB-stored vector can't be ORDER BY <-> ranked). Otherwise the
        category/color heuristic, which is the deterministic CI path."""
        k = 5
        if _pgvector_enabled():
            embedded = await self._by_embedding(user_id, candidate, k, locale)
            if embedded:
                return embedded
        return await self._by_category_color(user_id, candidate, k, locale)

    async def _by_embedding(
        self, user_id: uuid.UUID, candidate: VisionResult, k: int, locale: str
    ) -> list[SimilarItem]:
        """Rank same-category wardrobe items by cosine distance to the
        candidate's nearest already-embedded sibling. We have no vector for the
        unowned candidate, so we anchor on any wearable item of the same
        category+color and rank its neighbours — degrades to empty (→ fallback)
        when no anchor/embeddings exist."""
        from app.bootstrap.container import embedding_model_version

        current_vector = is_current_embedding(embedding_model_version())
        anchor = (
            await self.session.execute(
                select(WardrobeItem.id, ItemEmbedding.vector)
                .join(ItemEmbedding, ItemEmbedding.item_id == WardrobeItem.id)
                .where(
                    WardrobeItem.user_id == user_id,
                    WardrobeItem.category == candidate.category,
                    WardrobeItem.status == ItemStatus.WEARABLE.value,
                    WardrobeItem.deleted_at.is_(None),
                    current_vector,
                )
                .order_by((WardrobeItem.color_main == candidate.color_main).desc(), WardrobeItem.id)
                .limit(1)
            )
        ).one_or_none()
        if anchor is None:
            return []
        anchor_id, anchor_vec = anchor
        stmt = (
            select(
                WardrobeItem,
                ItemEmbedding.vector.op("<->", return_type=Float())(anchor_vec).label("dist"),
            )
            .join(ItemEmbedding, ItemEmbedding.item_id == WardrobeItem.id)
            .where(
                WardrobeItem.user_id == user_id,
                WardrobeItem.category == candidate.category,
                WardrobeItem.status == ItemStatus.WEARABLE.value,
                WardrobeItem.deleted_at.is_(None),
                WardrobeItem.id != anchor_id,
                current_vector,
            )
            .order_by("dist", WardrobeItem.id)
            .limit(k)
        )
        rows = (await self.session.execute(stmt)).all()
        scored: list[tuple[WardrobeItem, float]] = []
        for item, dist in rows:
            # Map L2 distance → a bounded similarity in (0, 1]; monotone decreasing.
            similarity = round(1.0 / (1.0 + float(dist)), 4)
            scored.append((item, similarity))
        return await self._build_similar_items(scored, locale)

    async def _by_category_color(
        self, user_id: uuid.UUID, candidate: VisionResult, k: int, locale: str
    ) -> list[SimilarItem]:
        rows = (
            await self.session.execute(
                select(WardrobeItem)
                .where(
                    WardrobeItem.user_id == user_id,
                    WardrobeItem.category == candidate.category,
                    WardrobeItem.status == ItemStatus.WEARABLE.value,
                    WardrobeItem.deleted_at.is_(None),
                )
                .order_by(WardrobeItem.created_at.desc())
            )
        ).scalars().all()
        # Same category + same color ranks above same-category-only.
        color_match = [
            (item, SIM_CATEGORY_COLOR)
            for item in rows
            if item.color_main == candidate.color_main
        ]
        other = [
            (item, _SIM_CATEGORY_ONLY)
            for item in rows
            if item.color_main != candidate.color_main
        ]
        return await self._build_similar_items((color_match + other)[:k], locale)

    async def _build_similar_items(
        self, scored: list[tuple[WardrobeItem, float]], locale: str
    ) -> list[SimilarItem]:
        if not scored:
            return []
        sources = await load_wardrobe_image_sources(self.session, [item for item, _ in scored])
        out: list[SimilarItem] = []
        for item, similarity in scored:
            object_key, fallback_url = sources[item.id][0]
            out.append(
                SimilarItem(
                    item_id=item.id,
                    similarity=similarity,
                    name=localized_item_name(
                        title=item.title,
                        color_main=item.color_main,
                        category=item.category,
                        locale=locale,
                        localized_names=item.localized_names
                        if isinstance(item.localized_names, dict)
                        else None,
                        name_source=item.name_source,
                    ),
                    category=item.category,
                    color_main=item.color_main,
                    thumbnail_object_key=object_key,
                    thumbnail_fallback_url=fallback_url,
                )
            )
        return out

    async def _wardrobe_gap(self, user_id: uuid.UUID) -> list[str]:
        """`Category` values for which the user owns zero wearable items."""
        rows = (
            await self.session.execute(
                select(WardrobeItem.category)
                .where(
                    WardrobeItem.user_id == user_id,
                    WardrobeItem.status == ItemStatus.WEARABLE.value,
                    WardrobeItem.deleted_at.is_(None),
                )
                .distinct()
            )
        ).scalars().all()
        owned = set(rows)
        return [c.value for c in Category if c.value not in owned]

    async def _outfit_potential(self, user_id: uuid.UUID, candidate: VisionResult) -> int:
        """Count wearable items in categories complementary to the candidate,
        capped — a rough "how many outfits could this join" signal."""
        complements = _COMPLEMENTS.get(candidate.category, ())
        if not complements:
            return 0
        count = await self.session.scalar(
            select(func.count(WardrobeItem.id)).where(
                WardrobeItem.user_id == user_id,
                WardrobeItem.category.in_(complements),
                WardrobeItem.status == ItemStatus.WEARABLE.value,
                WardrobeItem.deleted_at.is_(None),
            )
        )
        return min(int(count or 0), _OUTFIT_POTENTIAL_CAP)

    def _decide(
        self,
        candidate: VisionResult,
        similar: list[SimilarItem],
        gap: list[str],
        outfit_potential: int,
    ) -> tuple[str, I18nMessage, int]:
        """Rule-based verdict (no LLM).

        - skip: already owns a near-identical item (same category + color).
        - buy: fills a category gap, or pairs with many existing items.
        - consider: everything else.
        """
        near_duplicates = [s for s in similar if s.similarity >= SIM_CATEGORY_COLOR]
        near_duplicate_count = len(near_duplicates)
        fills_gap = candidate.category in gap

        if near_duplicates and not fills_gap:
            params = {
                "count": near_duplicate_count,
                "color": candidate.color_main,
                "category": candidate.category,
            }
            return (
                "skip",
                make_message(
                    "sense_check.skip.duplicate",
                    (
                        f"你已经有 {near_duplicate_count} 件相似的 "
                        f"{candidate.color_main} {candidate.category}，这件大概率会重复。"
                    ),
                    params,
                ),
                near_duplicate_count,
            )
        if fills_gap:
            params = {"category": candidate.category}
            return (
                "buy",
                make_message(
                    "sense_check.buy.gap",
                    f"你的衣橱里还没有 {candidate.category}，这件可以补上一个空缺。",
                    params,
                ),
                near_duplicate_count,
            )
        if outfit_potential >= 3:
            params = {"category": candidate.category, "count": outfit_potential}
            return (
                "buy",
                make_message(
                    "sense_check.buy.versatile",
                    (
                        f"这件 {candidate.category} 可以和 {outfit_potential} 件现有单品搭配，"
                        "能带来明显的搭配变化。"
                    ),
                    params,
                ),
                near_duplicate_count,
            )
        params = {"category": candidate.category, "count": outfit_potential}
        return (
            "consider",
            make_message(
                "sense_check.consider.some",
                (
                    f"你已经有一些 {candidate.category}，"
                    f"它还能和 {outfit_potential} 件现有单品搭配。"
                ),
                params,
            ),
            near_duplicate_count,
        )

    async def _disliked_colors(self, user_id: uuid.UUID) -> set[str]:
        """BE#69 — colors the user explicitly dislikes (lowercased). Gated by
        ENABLE_PREFERENCE_MEMORY; fail-soft so a read error never blocks a verdict."""
        if not get_settings().ENABLE_PREFERENCE_MEMORY:
            return set()
        try:
            from app.infrastructure.persistence.repositories.preference_memory_repo import (
                SqlAlchemyPreferenceMemoryRepository,
            )

            return await live_negative_values_by_type(
                SqlAlchemyPreferenceMemoryRepository(self.session),
                user_id,
                "color",
                "negative_preference",
            )
        except Exception:
            return set()

    async def _outfit_samples(
        self, user_id: uuid.UUID, candidate: VisionResult
    ) -> list[dict[str, Any]]:
        """Deterministic grounded samples: pair the candidate with the top owned
        item in each complementary category. Up to `_MAX_OUTFIT_SAMPLES` distinct
        looks (rotating to the next item per category). Real owned ids only."""
        complements = _COMPLEMENTS.get(candidate.category, ())
        if not complements:
            return []
        by_category: dict[str, list[WardrobeItem]] = {}
        for category in complements:
            rows = (
                await self.session.execute(
                    select(WardrobeItem)
                    .where(
                        WardrobeItem.user_id == user_id,
                        WardrobeItem.category == category,
                        WardrobeItem.status == ItemStatus.WEARABLE.value,
                        WardrobeItem.deleted_at.is_(None),
                    )
                    .order_by(WardrobeItem.created_at.desc())
                    .limit(_MAX_OUTFIT_SAMPLES)
                )
            ).scalars().all()
            if rows:
                by_category[category] = list(rows)
        if not by_category:
            return []
        samples: list[dict[str, Any]] = []
        for rank in range(_MAX_OUTFIT_SAMPLES):
            picks = [items[rank] for items in by_category.values() if rank < len(items)]
            if not picks:
                break
            samples.append(
                {
                    "item_ids": [str(item.id) for item in picks],
                    "reasoning": [f"搭配你现有的{item.category}" for item in picks],
                }
            )
        return samples

    async def _evaluate(
        self,
        user_id: uuid.UUID,
        candidate: VisionResult,
        *,
        similar: list[SimilarItem],
        gap: list[str],
        outfit_potential: int,
        verdict: str,
        display_reason: I18nMessage,
        price: float | None,
    ) -> tuple[str, I18nMessage, dict[str, Any]]:
        """BE#69 — build the wardrobe-aware evaluation block and apply the only
        verdict adjustment (a negative-preference conflict softens a `buy`)."""
        top_similarity = max((s.similarity for s in similar), default=0.0)
        if top_similarity >= _REDUNDANCY_HIGH:
            level = "high"
        elif top_similarity >= _REDUNDANCY_MEDIUM:
            level = "medium"
        else:
            level = "low"
        redundancy = {
            "level": level,
            "similar_owned_items": [
                {
                    "item_id": str(s.item_id),
                    "similarity": s.similarity,
                    "reason": "同类同色" if s.similarity >= SIM_CATEGORY_COLOR else "同类",
                }
                for s in similar
            ],
        }

        disliked = await self._disliked_colors(user_id)
        preference_conflict = bool(
            candidate.color_main and candidate.color_main.lower() in disliked
        )
        # The conflict softens an otherwise-positive verdict (never hardens), and
        # is always surfaced as a factor regardless of the verdict.
        if preference_conflict and verdict == "buy":
            verdict = "consider"
            display_reason = make_message(
                "sense_check.consider.preference_conflict",
                f"你之前表示不喜欢{candidate.color_main}，买之前先斟酌一下。",
                {"color": candidate.color_main, "category": candidate.category},
            )

        fills_gap = candidate.category in gap
        factors: list[dict[str, str]] = [
            {"factor": "redundancy", "detail": level, "direction": "skip" if level == "high" else "neutral"},
            {
                "factor": "outfit_potential",
                "detail": str(outfit_potential),
                "direction": "buy" if outfit_potential >= 3 else "neutral",
            },
        ]
        if fills_gap:
            factors.append({"factor": "wardrobe_gap", "detail": candidate.category, "direction": "buy"})
        if preference_conflict:
            factors.append(
                {"factor": "preference_conflict", "detail": candidate.color_main, "direction": "skip"}
            )

        confidence = self._confidence(verdict, level, fills_gap, preference_conflict=preference_conflict)
        wardrobe_gap_detail = [
            {"category": c, "severity": "high" if c in _CORE_CATEGORIES else "medium"} for c in gap
        ]
        evaluation: dict[str, Any] = {
            "confidence": confidence,
            "redundancy": redundancy,
            "outfit_samples": await self._outfit_samples(user_id, candidate),
            "wardrobe_gap_detail": wardrobe_gap_detail,
            "factors": factors,
            "cost_per_wear_hint": self._cost_per_wear_hint(price),
        }
        return verdict, display_reason, evaluation

    @staticmethod
    def _confidence(
        verdict: str, redundancy_level: str, fills_gap: bool, *, preference_conflict: bool
    ) -> float:
        if verdict == "skip" and redundancy_level == "high":
            base = 0.85
        elif verdict == "buy" and fills_gap:
            base = 0.85
        elif verdict == "buy":
            base = 0.7
        else:
            base = 0.5
        if preference_conflict:
            base -= 0.15
        return round(max(0.3, min(1.0, base)), 2)

    @staticmethod
    def _cost_per_wear_hint(price: float | None) -> str | None:
        if price is None or price <= 0:
            return None
        return f"按 {price:g} 元计，若每月至少穿 2 次，长期每次成本会很低；很少穿则不划算。"

    async def _persist(
        self,
        user_id: uuid.UUID,
        image_url: str | None,
        source_url: str | None,
        result: SenseCheckResult,
        object_key: str | None,
    ) -> ShoppingSenseCheck:
        payload = {
            "verdict": result.verdict,
            "reason": result.reason,
            "display_reason": result.display_reason.model_dump(mode="json"),
            "similar_item_count": result.similar_item_count,
            "similar_items": [
                {
                    "item_id": str(s.item_id),
                    "similarity": s.similarity,
                    "name": s.name,
                    "category": s.category,
                    "color_main": s.color_main,
                    "thumbnail_object_key": s.thumbnail_object_key,
                    "thumbnail_fallback_url": s.thumbnail_fallback_url,
                }
                for s in result.similar_items
            ],
            "wardrobe_gap": result.wardrobe_gap,
            "outfit_potential": result.outfit_potential,
            "candidate": {"category": result.category, "color_main": result.color_main},
            # BE#69 — present only when ENABLE_PURCHASE_EVALUATOR; replayed by /history.
            "evaluation": result.evaluation,
        }
        record = ShoppingSenseCheck(
            user_id=user_id,
            image_url=image_url,
            object_key=object_key,
            source_url=source_url,
            verdict=result.verdict,
            reason=result.reason,
            payload=payload,
        )
        self.session.add(record)
        await self.session.flush()
        return record


class ListSenseChecks:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def execute(
        self, user_id: uuid.UUID, *, limit: int = 20, offset: int = 0
    ) -> list[ShoppingSenseCheck]:
        rows = (
            await self.session.execute(
                select(ShoppingSenseCheck)
                .where(ShoppingSenseCheck.user_id == user_id)
                .order_by(ShoppingSenseCheck.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        ).scalars().all()
        return list(rows)
