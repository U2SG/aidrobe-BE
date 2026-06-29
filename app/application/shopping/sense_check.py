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

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports.vision import VisionPort, VisionResult
from app.common import error_codes
from app.common.enums import Category, ImageType, ItemStatus
from app.common.exceptions import FriendlyException
from app.common.i18n import I18nMessage, make_message
from app.common.item_labels import localized_item_name
from app.common.locale import resolve_locale
from app.config.settings import get_settings
from app.infrastructure.importers.link_parser import _is_safe_url
from app.infrastructure.persistence.models.embedding import ItemEmbedding
from app.infrastructure.persistence.models.shopping import ShoppingSenseCheck
from app.infrastructure.persistence.models.user import UserSettings
from app.infrastructure.persistence.models.wardrobe import WardrobeItem, WardrobeItemImage
from app.infrastructure.storage.object_key import extract_internal_object_key

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
    ) -> tuple[SenseCheckResult, ShoppingSenseCheck]:
        if image_url is None and source_url is None:
            raise FriendlyException(
                error_codes.VALIDATION_ERROR,
                "image_url or source_url is required",
                status_code=422,
            )
        if image_url is not None:
            if not _is_safe_url(image_url):
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
            vision_result = await self.vision.analyze_image(analyze_target)
        except (ValueError, RuntimeError) as exc:
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
        )
        record = await self._persist(user_id, image_url, source_url, result)
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
        anchor_id = await self.session.scalar(
            select(WardrobeItem.id)
            .where(
                WardrobeItem.user_id == user_id,
                WardrobeItem.category == candidate.category,
                WardrobeItem.status == ItemStatus.WEARABLE.value,
                WardrobeItem.deleted_at.is_(None),
            )
            .limit(1)
        )
        if anchor_id is None:
            return []
        anchor_vec = await self.session.scalar(
            select(ItemEmbedding.vector).where(ItemEmbedding.item_id == anchor_id).limit(1)
        )
        if anchor_vec is None:
            return []
        stmt = (
            select(
                WardrobeItem,
                ItemEmbedding.vector.op("<->")(anchor_vec).label("dist"),
            )
            .join(ItemEmbedding, ItemEmbedding.item_id == WardrobeItem.id)
            .where(
                WardrobeItem.user_id == user_id,
                WardrobeItem.category == candidate.category,
                WardrobeItem.status == ItemStatus.WEARABLE.value,
                WardrobeItem.deleted_at.is_(None),
                WardrobeItem.id != anchor_id,
            )
            .order_by("dist")
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
        sources = await self._thumbnail_sources([item.id for item, _ in scored])
        out: list[SimilarItem] = []
        for item, similarity in scored:
            object_key, fallback_url = sources.get(item.id, (None, item.cover_image_url))
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

    async def _thumbnail_sources(
        self, item_ids: list[uuid.UUID]
    ) -> dict[uuid.UUID, tuple[str | None, str | None]]:
        images = list(
            await self.session.scalars(
                select(WardrobeItemImage).where(WardrobeItemImage.item_id.in_(item_ids))
            )
        )
        preferred: dict[uuid.UUID, WardrobeItemImage] = {}
        for image in images:
            existing = preferred.get(image.item_id)
            if existing is None or (
                existing.image_type != ImageType.STANDARD.value
                and image.image_type == ImageType.STANDARD.value
            ):
                preferred[image.item_id] = image
        return {
            item_id: (image.object_key, image.image_url)
            for item_id, image in preferred.items()
        }

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

    async def _persist(
        self,
        user_id: uuid.UUID,
        image_url: str | None,
        source_url: str | None,
        result: SenseCheckResult,
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
        }
        record = ShoppingSenseCheck(
            user_id=user_id,
            image_url=image_url,
            object_key=extract_internal_object_key(image_url, get_settings()),
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
