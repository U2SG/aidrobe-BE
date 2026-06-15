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
from app.common.enums import Category, ItemStatus
from app.common.exceptions import FriendlyException
from app.infrastructure.importers.link_parser import _is_safe_url
from app.infrastructure.persistence.models.embedding import ItemEmbedding
from app.infrastructure.persistence.models.shopping import ShoppingSenseCheck
from app.infrastructure.persistence.models.wardrobe import WardrobeItem

# Similarity scores assigned by the deterministic fallback path. Real cosine
# scores (pgvector branch) override these when available.
_SIM_CATEGORY_COLOR = 0.9
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


@dataclass
class SenseCheckResult:
    verdict: str  # buy / consider / skip
    reason: str
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
            from app.config.settings import get_settings
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

        similar = await self._find_similar(user_id, vision_result)
        gap = await self._wardrobe_gap(user_id)
        outfit_potential = await self._outfit_potential(user_id, vision_result)
        verdict, reason = self._decide(vision_result, similar, gap, outfit_potential)

        result = SenseCheckResult(
            verdict=verdict,
            reason=reason,
            similar_items=similar,
            wardrobe_gap=gap,
            outfit_potential=outfit_potential,
            category=vision_result.category,
            color_main=vision_result.color_main,
        )
        record = await self._persist(user_id, image_url, source_url, result)
        return result, record

    async def _find_similar(
        self, user_id: uuid.UUID, candidate: VisionResult
    ) -> list[SimilarItem]:
        """Up to K wardrobe items most similar to the candidate.

        Embedding branch only when pgvector is on (mirrors replacements.py —
        the JSONB-stored vector can't be ORDER BY <-> ranked). Otherwise the
        category/color heuristic, which is the deterministic CI path."""
        k = 5
        if _pgvector_enabled():
            embedded = await self._by_embedding(user_id, candidate, k)
            if embedded:
                return embedded
        return await self._by_category_color(user_id, candidate, k)

    async def _by_embedding(
        self, user_id: uuid.UUID, candidate: VisionResult, k: int
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
                WardrobeItem.id,
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
        out: list[SimilarItem] = []
        for row in rows:
            # Map L2 distance → a bounded similarity in (0, 1]; monotone decreasing.
            similarity = round(1.0 / (1.0 + float(row.dist)), 4)
            out.append(SimilarItem(item_id=row.id, similarity=similarity))
        return out

    async def _by_category_color(
        self, user_id: uuid.UUID, candidate: VisionResult, k: int
    ) -> list[SimilarItem]:
        rows = (
            await self.session.execute(
                select(WardrobeItem.id, WardrobeItem.color_main)
                .where(
                    WardrobeItem.user_id == user_id,
                    WardrobeItem.category == candidate.category,
                    WardrobeItem.status == ItemStatus.WEARABLE.value,
                    WardrobeItem.deleted_at.is_(None),
                )
                .order_by(WardrobeItem.created_at.desc())
            )
        ).all()
        # Same category + same color ranks above same-category-only.
        color_match = [
            SimilarItem(item_id=r.id, similarity=_SIM_CATEGORY_COLOR)
            for r in rows
            if r.color_main == candidate.color_main
        ]
        other = [
            SimilarItem(item_id=r.id, similarity=_SIM_CATEGORY_ONLY)
            for r in rows
            if r.color_main != candidate.color_main
        ]
        return (color_match + other)[:k]

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
    ) -> tuple[str, str]:
        """Rule-based verdict (no LLM).

        - skip: already owns a near-identical item (same category + color).
        - buy: fills a category gap, or pairs with many existing items.
        - consider: everything else.
        """
        near_duplicates = [s for s in similar if s.similarity >= _SIM_CATEGORY_COLOR]
        fills_gap = candidate.category in gap

        if near_duplicates and not fills_gap:
            return (
                "skip",
                f"You already own {len(near_duplicates)} similar {candidate.color_main} "
                f"{candidate.category} item(s); this would likely duplicate your wardrobe.",
            )
        if fills_gap:
            return (
                "buy",
                f"You have no {candidate.category} yet — this fills a gap in your wardrobe.",
            )
        if outfit_potential >= 3:
            return (
                "buy",
                f"This {candidate.category} pairs with {outfit_potential}+ existing items, "
                "adding real outfit variety.",
            )
        return (
            "consider",
            f"You own some {candidate.category} items already; "
            f"it can pair with {outfit_potential} of your current pieces.",
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
            "similar_items": [
                {"item_id": str(s.item_id), "similarity": s.similarity}
                for s in result.similar_items
            ],
            "wardrobe_gap": result.wardrobe_gap,
            "outfit_potential": result.outfit_potential,
            "candidate": {"category": result.category, "color_main": result.color_main},
        }
        record = ShoppingSenseCheck(
            user_id=user_id,
            image_url=image_url,
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
