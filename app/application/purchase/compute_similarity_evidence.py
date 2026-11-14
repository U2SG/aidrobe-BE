"""V3-005 — load the wardrobe a candidate is compared against, then rank it.

The DB half of the feature; all judgement lives in `wardrobe_similarity.py`.
Nothing here is exposed: V3-005 registers no router and changes no schema, so
`docs/openapi.json` is untouched and `ENABLE_V3_PURCHASE_DECISION` (V3-003)
still gates every V3 surface. V3-006 composes outfits from this evidence and
V3-007 turns it into a decision and freezes it into `evidence_snapshot`.

**Same-category items are never displaceable by complements.** The scan is two
*independently* bounded windows — one over the candidate's own category, one
over its complements — combined with `UNION ALL` into a single statement. The
first version of this module used one `LIMIT` across both groups, which meant a
wardrobe whose most recent items were all complements could load zero
same-category items and then report the *confident* `redundancy_level="none"`
to someone who owns ten similar coats. That is a wrong answer in the
buy-encouraging direction, so the bound is per group and `none` now means "the
same-category window was queried and came back empty". Whether the
same-category window saturated is reported to the caller, because a saturated
window makes `similar_item_count` a floor rather than a total.

**Query budget** (V3-005 requires query-count evidence). Constant in the size
of the wardrobe — the count must not grow with the number of owned items, or a
large closet would turn one decision into hundreds of round-trips:

- 1 — the wardrobe scan. Two bounded windows, one statement: splitting the
      windows costs no extra round-trip, which is why the per-group bound is
      not a trade against this budget.
- 1 — distinct `fit_feedback.scene` per item, batched with `IN` over the ids
      from the first query. This is the N+1 that a naive per-item scene lookup
      would introduce.
- at most +1 — only when pgvector is on: find the current embedding anchor
      and rank its neighbours in one statement.

So 2 queries in the deterministic path and at most 3 with pgvector, whether
the user owns three items or three hundred. Those exact per-call counts are pinned in
`tests/unit/test_v3_similarity_query_count.py` against a session double, which
runs without a container. `tests/services/test_v3_similarity_evidence.py`
re-checks the same invariant against a real Postgres, but as "constant in
wardrobe size, and bounded" rather than as one exact number — the SQL a driver
emits around a statement is not this module's contract.

The row bound the scan hands to the scene batch is therefore
`2 * MAX_ITEMS_PER_CATEGORY_GROUP`, not `MAX_ITEMS_PER_CATEGORY_GROUP`.

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

import uuid

import structlog
from sqlalchemy import Float, Select, select, true, union_all
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.application.purchase.candidate_normalizer import ParsedItem
from app.application.purchase.garment_type import garment_type
from app.application.purchase.wardrobe_similarity import (
    COMPLEMENTARY_CATEGORIES,
    ClosetEvidence,
    OwnedItemFacts,
    build_evidence,
    normalize_style_tags,
    rank_by_attributes,
    unknown_evidence,
)
from app.common.enums import FitFeedbackType, ItemStatus, WardrobeItemStatus
from app.config.embedding import embedding_model_version
from app.infrastructure.persistence.models.embedding import ItemEmbedding, is_current_embedding, pgvector_enabled
from app.infrastructure.persistence.models.fit import FitFeedback
from app.infrastructure.persistence.models.wardrobe import WardrobeItem

logger = structlog.get_logger(__name__)

# Upper bound on rows pulled into memory for ranking, applied **per category
# group** (see the module docstring). A wardrobe larger than this still works —
# it just compares against the most recent slice of each group, which is also
# the slice the user is most likely to think of as "what I own".
MAX_ITEMS_PER_CATEGORY_GROUP = 200

# Statuses that mean "the user still owns this". Deliberately wider than the
# `status == wearable` filter that `outfits/replacements.py` and
# `shopping/sense_check.py` use, and the difference is the question being asked:
# those pick something to *wear today*, and a dirty coat cannot be worn today.
# V3-005 asks whether the user *already owns* something like the candidate, and
# a coat in the laundry basket is still a coat they own — `dirty` is an
# explicitly temporary state with a documented transition back to `wearable`
# (`docs/wardrobe-status-machine.md`). Counting it as absent would understate
# redundancy in the buy-encouraging direction.
#
# `lost` and `donated` stay out: those are no longer owned. `unavailable` also
# stays out — the status machine gives it no documented meaning beyond "not
# currently wearable", so reading it as owned would be a guess, and V3-005 is
# not the place to assign it one.
OWNED_STATUSES: tuple[str, ...] = (
    ItemStatus.WEARABLE.value,
    WardrobeItemStatus.DIRTY.value,
)


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
    complements = COMPLEMENTARY_CATEGORIES.get(category, ())

    items = await _load_items(session, user_id, category, complements)
    if not items:
        # An empty scan is never a truncated one, so the default flag is right.
        return build_evidence(parsed, [])

    # A saturated same-category window means the count is a floor, not a total.
    # Derived from the rows already in hand, so it costs no extra query — which
    # is why the per-group bound is not a trade against the query budget.
    same_category_truncated = (
        sum(1 for item in items if item.category == category) >= MAX_ITEMS_PER_CATEGORY_GROUP
    )

    scenes = await load_wear_scenes(session, [item.id for item in items])
    facts = [
        OwnedItemFacts(
            item_id=item.id,
            category=item.category,
            color_main=item.color_main or "",
            material=item.material,
            garment_type=(
                garment_type(item.title, item.sub_category)
                if item.name_source == "user"
                else garment_type(item.canonical_name, item.sub_category, item.title)
            ),
            style_tags=normalize_style_tags(item.style_tags),
            wear_scenes=tuple(sorted(scenes.get(item.id, set()))),
        )
        for item in items
    ]

    ranked = await _rank(session, user_id, category, facts, parsed)
    return build_evidence(parsed, ranked, same_category_truncated=same_category_truncated)


def build_wardrobe_scan_statement(
    user_id: uuid.UUID,
    category: str,
    complements: tuple[str, ...],
) -> Select[tuple[WardrobeItem]]:
    """Query 1 — two independently bounded windows in one statement.

    Public so `tests/unit/test_v3_similarity_scan_window.py` can assert the
    per-group bound by compiling the statement, which needs no container. The
    invariant it pins: the same-category window has its own `LIMIT`, so no
    number of complements can push same-category items out of the scan.

    Only items the user still owns and has not soft-deleted are compared —
    claiming redundancy against something they gave away is a claim they cannot
    act on. See `OWNED_STATUSES` for why that is wider than `wearable`.

    The outer `ORDER BY` is not decoration: Postgres does not carry a leg's
    ordering through `UNION ALL`, and `order_by_ids` appends its unranked tail
    in scan order, so without it the pgvector path would return a different
    order run to run and acceptance 1's determinism would be lost.
    """

    def _window(categories: tuple[str, ...]) -> Select[tuple[WardrobeItem]]:
        return (
            select(WardrobeItem)
            .where(
                WardrobeItem.user_id == user_id,
                WardrobeItem.category.in_(categories),
                WardrobeItem.status.in_(OWNED_STATUSES),
                WardrobeItem.deleted_at.is_(None),
            )
            .order_by(WardrobeItem.created_at.desc(), WardrobeItem.id)
            .limit(MAX_ITEMS_PER_CATEGORY_GROUP)
        )

    same_category = _window((category,))
    if not complements:
        # Nothing to displace it with, so the single window *is* the per-group
        # window. Avoids a pointless `UNION ALL` against an empty `IN ()`.
        return same_category
    scan = union_all(same_category, _window(complements)).subquery()
    owned = aliased(WardrobeItem, scan)
    return select(owned).order_by(owned.created_at.desc(), owned.id)


async def _load_items(
    session: AsyncSession,
    user_id: uuid.UUID,
    category: str,
    complements: tuple[str, ...],
) -> list[WardrobeItem]:
    rows = await session.scalars(build_wardrobe_scan_statement(user_id, category, complements))
    return list(rows)


async def load_wear_scenes(
    session: AsyncSession,
    item_ids: list[uuid.UUID],
) -> dict[uuid.UUID, set[str]]:
    """Query 2 — the scenes each item was actually rated in, batched.

    `fit_feedback` is the only per-item scene signal that exists in this
    service, and it is sparse: most items have no row, which is precisely why
    `wardrobe_similarity` carries a labelled fallback below it.

    Public because V3-006 needs the same signal over its own (narrower) item
    set, and a second copy of this query would be a second chance to reintroduce
    the per-item N+1 this one exists to prevent.

    Negative comfort judgments are excluded: "I wore it there and it was
    uncomfortable / not flattering" is a reason NOT to boost the item in that
    scene, and counting it as positive `wear_scenes` evidence let explicitly
    disliked items outrank neutral ones exactly where the user rejected them
    (codex review, BE#141). `tight`/`loose` stay in — they are size signals,
    not a rejection of the scene.
    """
    if not item_ids:
        return {}
    negative = (FitFeedbackType.UNCOMFORTABLE.value, FitFeedbackType.NOT_FLATTERING.value)
    rows = await session.execute(
        select(FitFeedback.item_id, FitFeedback.scene)
        .where(FitFeedback.item_id.in_(item_ids))
        .where(FitFeedback.feedback_type.notin_(negative))
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
    if pgvector_enabled():
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
    """Query 3 — nearest neighbours of an owned anchor item.

    There is no vector for the candidate itself: it is not owned, so nothing
    ever embedded it. The workaround is `sense_check._by_embedding`'s — anchor
    on an owned item of the same category and rank its neighbours, which
    answers "what in this category clusters together" even without a candidate
    vector. Returns `[]` (→ structured fallback) when no current anchor exists.

    Only same-category items are vector-ranked; complements keep their
    structured order appended after, since a cross-category cosine distance
    would rank shoes against a coat on image features alone.
    """

    same_category = [f.item_id for f in facts if f.category == category]
    if not same_category:
        return []
    current_vector = is_current_embedding(embedding_model_version())
    # The `ORDER BY` makes the anchor arbitrary but *stable*. Without it the
    # entire vector ranking hangs on whichever row Postgres happened to return,
    # so the same wardrobe could rank two ways — and V3-007 freezes this output
    # into an immutable snapshot, where a run-to-run difference is permanent.
    # Ordering on the id matches `rank_by_attributes`' tie-break convention.
    eligibility = (
        WardrobeItem.user_id == user_id,
        WardrobeItem.category == category,
        WardrobeItem.status.in_(OWNED_STATUSES),
        WardrobeItem.deleted_at.is_(None),
        WardrobeItem.id.in_(same_category),
        current_vector,
    )
    anchor = (
        select(ItemEmbedding.vector)
        .join(WardrobeItem, WardrobeItem.id == ItemEmbedding.item_id)
        .where(*eligibility)
        .order_by(ItemEmbedding.item_id)
        .limit(1)
    ).cte("purchase_anchor")
    ranked_ids = list(
        await session.scalars(
            select(WardrobeItem.id)
            .join(ItemEmbedding, ItemEmbedding.item_id == WardrobeItem.id)
            .join(anchor, true())
            .where(*eligibility)
            .order_by(ItemEmbedding.vector.op("<->", return_type=Float())(anchor.c.vector), WardrobeItem.id)
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
