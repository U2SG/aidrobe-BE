"""V3-005 — load the wardrobe a candidate is compared against, then rank it.

The DB half of the feature; all judgement lives in `wardrobe_similarity.py`.
Nothing here is exposed: V3-005 registers no router and changes no schema, so
`docs/openapi.json` is untouched and `ENABLE_V3_PURCHASE_DECISION` (V3-003)
still gates every V3 surface. V3-006 composes outfits from this evidence and
V3-007 turns it into a decision and freezes it into `evidence_snapshot`.

**Query budget** (V3-005 requires query-count evidence). Constant in the size
of the wardrobe — the count must not grow with the number of owned items, or a
large closet would turn one decision into hundreds of round-trips:

- 1 — wardrobe items in the candidate's category plus its complements.
- 1 — distinct `fit_feedback.scene` per item, batched with `IN` over the ids
      from the first query. This is the N+1 that a naive per-item scene lookup
      would introduce.
- +2 — only when pgvector is on: the embedding anchor, then the vector rank.

So 2 queries in the deterministic path and 4 in the vector path, whether the
user owns three items or three hundred. `test_v3_similarity_evidence` asserts
those exact numbers against a counting event listener.

**Ranking cascade**, mirroring `outfits/replacements.py` and
`shopping/sense_check.py` rather than inventing a third convention:

    pgvector neighbours (when enabled) → structured attributes → done

The vector tier degrades to empty — not to an error — when no anchor item or
no embedding exists, which is exactly what happens on this project's CI
(`PGVECTOR_ENABLED` unset, `ItemEmbedding.vector` stored as JSONB). The
structured tier is therefore the path CI exercises, and it is deterministic by
construction so fixtures can assert an exact order.
"""

from __future__ import annotations

import os
import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.purchase.candidate_normalizer import ParsedItem
from app.application.purchase.wardrobe_similarity import (
    COMPLEMENTARY_CATEGORIES,
    ClosetEvidence,
    OwnedItemFacts,
    build_evidence,
    normalize_style_tags,
    rank_by_attributes,
    unknown_evidence,
)
from app.common.enums import ItemStatus
from app.infrastructure.persistence.models.embedding import ItemEmbedding
from app.infrastructure.persistence.models.fit import FitFeedback
from app.infrastructure.persistence.models.wardrobe import WardrobeItem

logger = structlog.get_logger(__name__)

# Upper bound on rows pulled into memory for ranking. A wardrobe larger than
# this still works — it just compares against the most recent slice, which is
# also the slice the user is most likely to think of as "what I own".
MAX_ITEMS_CONSIDERED = 200


def _pgvector_enabled() -> bool:
    """Same convention as `models.embedding._USE_VECTOR` and
    `replacements._pgvector_enabled` — read the env directly rather than
    carrying a private import surface."""
    return os.getenv("PGVECTOR_ENABLED", "false").lower() in ("1", "true", "yes")


async def compute_similarity_evidence(
    session: AsyncSession,
    user_id: uuid.UUID,
    parsed: ParsedItem | None,
) -> ClosetEvidence:
    """Wardrobe similarity and redundancy evidence for one purchase candidate.

    Never raises on a degraded candidate: a parse that yielded no category
    (V3-004 `parse_failed`, or a partial parse) returns `unknown_evidence()`,
    because the decision flow has to keep working when parsing did not.
    """
    if parsed is None or not parsed.is_usable:
        return unknown_evidence()

    category = (parsed.category or "").strip().lower()
    categories = (category, *COMPLEMENTARY_CATEGORIES.get(category, ()))

    items = await _load_items(session, user_id, categories)
    if not items:
        return build_evidence(parsed, [])

    scenes = await _wear_scenes(session, [item.id for item in items])
    facts = [
        OwnedItemFacts(
            item_id=item.id,
            category=item.category,
            color_main=item.color_main or "",
            material=item.material,
            style_tags=normalize_style_tags(item.style_tags),
            wear_scenes=tuple(sorted(scenes.get(item.id, set()))),
        )
        for item in items
    ]

    ranked = await _rank(session, user_id, category, facts, parsed)
    return build_evidence(parsed, ranked)


async def _load_items(
    session: AsyncSession,
    user_id: uuid.UUID,
    categories: tuple[str, ...],
) -> list[WardrobeItem]:
    """Query 1 — the candidate's category and its complements, in one pass.

    Wearable and not soft-deleted only: comparing a purchase against something
    the user has thrown out or lost would produce a redundancy claim they
    cannot act on.
    """
    rows = await session.scalars(
        select(WardrobeItem)
        .where(
            WardrobeItem.user_id == user_id,
            WardrobeItem.category.in_(categories),
            WardrobeItem.status == ItemStatus.WEARABLE.value,
            WardrobeItem.deleted_at.is_(None),
        )
        .order_by(WardrobeItem.created_at.desc())
        .limit(MAX_ITEMS_CONSIDERED)
    )
    return list(rows)


async def _wear_scenes(
    session: AsyncSession,
    item_ids: list[uuid.UUID],
) -> dict[uuid.UUID, set[str]]:
    """Query 2 — the scenes each item was actually rated in, batched.

    `fit_feedback` is the only per-item scene signal that exists in this
    service, and it is sparse: most items have no row, which is precisely why
    `wardrobe_similarity` carries a labelled fallback below it.
    """
    if not item_ids:
        return {}
    rows = await session.execute(
        select(FitFeedback.item_id, FitFeedback.scene)
        .where(FitFeedback.item_id.in_(item_ids))
        .distinct()
    )
    scenes: dict[uuid.UUID, set[str]] = {}
    for item_id, scene in rows.all():
        if item_id is None or not scene:
            continue
        scenes.setdefault(item_id, set()).add(scene.strip().lower())
    return scenes


async def _rank(
    session: AsyncSession,
    user_id: uuid.UUID,
    category: str,
    facts: list[OwnedItemFacts],
    parsed: ParsedItem,
) -> list[OwnedItemFacts]:
    """Order owned items, vector tier first, structured tier as the fallback."""
    if _pgvector_enabled():
        ordered = await _rank_by_embedding(session, user_id, category, facts)
        if ordered:
            return ordered
        logger.info(
            "v3_similarity.fallback",
            from_tier="embedding",
            to_tier="structured_attributes",
            category=category,
        )
    return rank_by_attributes(parsed, facts)


async def _rank_by_embedding(
    session: AsyncSession,
    user_id: uuid.UUID,
    category: str,
    facts: list[OwnedItemFacts],
) -> list[OwnedItemFacts]:
    """Queries 3 and 4 — nearest neighbours of an owned anchor item.

    There is no vector for the candidate itself: it is not owned, so nothing
    ever embedded it. The workaround is `sense_check._by_embedding`'s — anchor
    on an owned item of the same category and rank its neighbours, which
    answers "what in this category clusters together" even without a candidate
    vector. Returns `[]` (→ structured fallback) whenever no anchor or no
    embedding exists, which is the case on CI.

    Only same-category items are vector-ranked; complements keep their
    structured order appended after, since a cross-category cosine distance
    would rank shoes against a coat on image features alone.
    """
    same_category = [f for f in facts if f.category == category]
    if not same_category:
        return []
    anchor_vector = await session.scalar(
        select(ItemEmbedding.vector)
        .where(ItemEmbedding.item_id.in_([f.item_id for f in same_category]))
        .limit(1)
    )
    if anchor_vector is None:
        return []
    ranked_ids = list(
        await session.scalars(
            select(WardrobeItem.id)
            .join(ItemEmbedding, ItemEmbedding.item_id == WardrobeItem.id)
            .where(
                WardrobeItem.user_id == user_id,
                WardrobeItem.category == category,
                WardrobeItem.status == ItemStatus.WEARABLE.value,
                WardrobeItem.deleted_at.is_(None),
            )
            .order_by(ItemEmbedding.vector.op("<->")(anchor_vector))
            .limit(len(same_category))
        )
    )
    if not ranked_ids:
        return []
    return order_by_ids(facts, ranked_ids)


def order_by_ids(
    facts: list[OwnedItemFacts],
    ranked_ids: list[uuid.UUID],
) -> list[OwnedItemFacts]:
    """Apply an externally-decided order, keeping unranked items after it.

    Split out and public so the vector tier's *output handling* is unit-testable
    without the extension: `test_v3_wardrobe_similarity` feeds it an order no
    structured rule would produce and asserts the payload follows it. The vector
    query itself needs a real pgvector database and is therefore covered by
    branch-selection and degradation tests rather than by a ranked result.
    """
    by_id = {f.item_id: f for f in facts}
    ordered = [by_id[i] for i in ranked_ids if i in by_id]
    ranked = set(ranked_ids)
    ordered.extend(f for f in facts if f.item_id not in ranked)
    return ordered
