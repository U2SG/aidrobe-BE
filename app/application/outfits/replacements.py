"""Phase D2 — replacement candidate ranking with degraded-mode cascade.

Used by `obstacles.detect_obstacles` to suggest "wear this instead" choices
when an item is dirty / lost / seasonal-mismatched. Plan §2.5:

    embedding similarity → same category+color → same category → null

Each step that has to fall back logs `replacement.fallback`. When every step
is empty, `replacement.exhausted` fires once. The query is intentionally
stateless and bounded — N candidates per obstacle, where N is small
(default 3), so detect_obstacles can call this per-obstacle without
blowing up request latency.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterable

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import ItemStatus
from app.infrastructure.persistence.models.embedding import ItemEmbedding
from app.infrastructure.persistence.models.wardrobe import WardrobeItem

logger = structlog.get_logger(__name__)

DEFAULT_K = 3


def _pgvector_enabled() -> bool:
    """Same convention as `models.embedding._USE_VECTOR` — read directly so
    we don't carry a private import surface."""
    return os.getenv("PGVECTOR_ENABLED", "false").lower() in ("1", "true", "yes")


async def find_replacements(
    session: AsyncSession,
    item: WardrobeItem,
    *,
    exclude_ids: Iterable[uuid.UUID] = (),
    k: int = DEFAULT_K,
) -> list[uuid.UUID]:
    """Return up to `k` wearable item IDs that could replace `item`. Empty
    when no candidate exists at any tier of the cascade."""
    if k <= 0:
        return []
    exclude = {item.id, *exclude_ids}
    user_id = item.user_id
    category = item.category

    # Tier 1: embedding similarity — only when pgvector is enabled (test env
    # falls back to JSONB storage, similarity ORDER BY <-> would error).
    if _pgvector_enabled():
        candidates = await _by_embedding(session, item, exclude, k)
        if candidates:
            return candidates
        logger.info(
            "replacement.fallback",
            item_id=str(item.id),
            from_tier="embedding",
            to_tier="category_color",
        )

    # Tier 2: same category + same color_main, freshest first.
    candidates = await _by_category_color(session, user_id, category, item.color_main, exclude, k)
    if candidates:
        return candidates
    logger.info(
        "replacement.fallback",
        item_id=str(item.id),
        from_tier="category_color",
        to_tier="category",
    )

    # Tier 3: same category only.
    candidates = await _by_category(session, user_id, category, exclude, k)
    if candidates:
        return candidates

    logger.warning("replacement.exhausted", item_id=str(item.id), category=category)
    return []


async def _by_embedding(
    session: AsyncSession,
    item: WardrobeItem,
    exclude: set[uuid.UUID],
    k: int,
) -> list[uuid.UUID]:
    """Cosine-distance neighbours of `item` within the same category. Requires
    pgvector + an `ItemEmbedding` row for the source item; otherwise empty
    and the cascade moves on."""
    src_embedding = await session.scalar(
        select(ItemEmbedding).where(ItemEmbedding.item_id == item.id).limit(1)
    )
    if src_embedding is None:
        return []
    src_vector = src_embedding.vector
    # `<->` is the L2 operator pgvector exposes. Cosine would be `<=>`; either
    # works for top-k ranking on normalized CLIP vectors. Use `<->` because
    # legacy migrations declared the index against the same operator.
    stmt = (
        select(WardrobeItem.id)
        .join(ItemEmbedding, ItemEmbedding.item_id == WardrobeItem.id)
        .where(
            WardrobeItem.user_id == item.user_id,
            WardrobeItem.category == item.category,
            WardrobeItem.status == ItemStatus.WEARABLE.value,
            WardrobeItem.deleted_at.is_(None),
            WardrobeItem.id.notin_(exclude),
        )
        .order_by(ItemEmbedding.vector.op("<->")(src_vector))
        .limit(k)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return list(rows)


async def _by_category_color(
    session: AsyncSession,
    user_id: uuid.UUID,
    category: str,
    color_main: str | None,
    exclude: set[uuid.UUID],
    k: int,
) -> list[uuid.UUID]:
    if not color_main:
        return []
    stmt = (
        select(WardrobeItem.id)
        .where(
            WardrobeItem.user_id == user_id,
            WardrobeItem.category == category,
            WardrobeItem.color_main == color_main,
            WardrobeItem.status == ItemStatus.WEARABLE.value,
            WardrobeItem.deleted_at.is_(None),
            WardrobeItem.id.notin_(exclude),
        )
        .order_by(WardrobeItem.created_at.desc())
        .limit(k)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return list(rows)


async def _by_category(
    session: AsyncSession,
    user_id: uuid.UUID,
    category: str,
    exclude: set[uuid.UUID],
    k: int,
) -> list[uuid.UUID]:
    stmt = (
        select(WardrobeItem.id)
        .where(
            WardrobeItem.user_id == user_id,
            WardrobeItem.category == category,
            WardrobeItem.status == ItemStatus.WEARABLE.value,
            WardrobeItem.deleted_at.is_(None),
            WardrobeItem.id.notin_(exclude),
        )
        .order_by(WardrobeItem.created_at.desc())
        .limit(k)
    )
    rows = (await session.execute(stmt)).scalars().all()
    return list(rows)


async def find_replacements_batch(
    session: AsyncSession,
    items: list[WardrobeItem],
    *,
    exclude_ids: Iterable[uuid.UUID] = (),
    k: int = DEFAULT_K,
) -> dict[uuid.UUID, list[uuid.UUID]]:
    """Find replacements for multiple items with one DB query rather than
    N. Returns a map item_id → top-K candidate ids. Skips the embedding
    tier (per-item by nature) and the strict color tier (would require
    multiple sub-queries to disambiguate); callers that get an empty
    list for an item should fall back to the per-item `find_replacements`
    so the cascade still bottoms out correctly.

    Used by `obstacles.attach_replacement_candidates` to avoid per-
    obstacle SQL on the /today hot path."""
    if not items:
        return {}
    exclude = {*exclude_ids, *(i.id for i in items)}
    user_id = items[0].user_id  # all items share a user
    categories = list({i.category for i in items})
    rows = (
        await session.execute(
            select(WardrobeItem.id, WardrobeItem.category, WardrobeItem.color_main)
            .where(
                WardrobeItem.user_id == user_id,
                WardrobeItem.category.in_(categories),
                WardrobeItem.status == ItemStatus.WEARABLE.value,
                WardrobeItem.deleted_at.is_(None),
                WardrobeItem.id.notin_(exclude),
            )
            .order_by(WardrobeItem.created_at.desc())
        )
    ).all()
    # Bucket candidates by (category, color) and (category) for in-memory
    # ranking matching the cascade order.
    by_cat_color: dict[tuple[str, str | None], list[uuid.UUID]] = {}
    by_cat: dict[str, list[uuid.UUID]] = {}
    for row in rows:
        key_full = (row.category, row.color_main)
        by_cat_color.setdefault(key_full, []).append(row.id)
        by_cat.setdefault(row.category, []).append(row.id)

    out: dict[uuid.UUID, list[uuid.UUID]] = {}
    for item in items:
        # Tier 2: same category + same color first.
        cands = by_cat_color.get((item.category, item.color_main), [])
        if len(cands) < k:
            # Tier 3: top up from same-category pool, preserving order
            # (created_at DESC) and avoiding duplicates.
            seen = set(cands)
            for cand in by_cat.get(item.category, []):
                if cand in seen:
                    continue
                cands.append(cand)
                seen.add(cand)
                if len(cands) >= k:
                    break
        out[item.id] = cands[:k]
    return out
