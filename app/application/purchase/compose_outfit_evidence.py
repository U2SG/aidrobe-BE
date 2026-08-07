"""V3-006 — load the wearable wardrobe, then compose grounded outfits from it.

The DB half of the feature; all judgement lives in `outfit_composition.py`.
Nothing here is exposed: V3-006 registers no router and changes no schema, so
`docs/openapi.json` is untouched and `ENABLE_V3_PURCHASE_DECISION` (V3-003)
still gates every V3 surface. V3-007 turns this evidence into a decision and
freezes it into `wardrobe_decisions.evidence_snapshot`.

**Available, not merely owned — and enforced in SQL.** `AVAILABLE_STATUSES` is
narrower than V3-005's `OWNED_STATUSES`: a `dirty` coat is still owned (so it
counts against redundancy) but cannot be worn tonight (so it must never be
suggested as a supporting item). Items whose `fit_status` is `no_longer_fits`
are excluded for the same reason, matching `outfits/replacements.py`, which is
the codebase's existing "what could you actually wear" filter. Doing this in
the WHERE clause rather than in Python is what stops unwearable rows from
consuming the scan window and hiding wearable ones behind it — the failure mode
V3-005's round-1 scan had.

**One window per category.** An outfit needs one item per slot, so a single
`LIMIT` across categories would let 200 recent tops hide every pair of shoes
and produce a confident "you would also need to buy shoes". The scan is
therefore one bounded window per relevant category, plus one pinned window for
V3-005's similar items, all combined with `UNION ALL` into a single statement —
splitting the windows costs no extra round-trip. The outer `ORDER BY` is
explicit because Postgres does not carry a leg's ordering through `UNION ALL`,
and the composition layer treats input order as the tie-break.

**Query budget** (constant in wardrobe size — the same N+1 guard V3-005 pins):

- 1 — the availability scan, all windows in one statement.
- 1 — distinct `fit_feedback.scene` for the scanned ids, batched.

So 2 queries whether the user owns three items or three hundred, pinned in
`tests/unit/test_v3_outfit_query_count.py` against a session double and
re-checked against real SQL in `tests/services/test_v3_outfit_evidence.py`.

There is no third tier here and no embedding query: V3-006 ranks *within* a
slot, which is a colour/scene judgement over items already loaded, not a
nearest-neighbour search. Acceptance 3 ("does not require a visual generation
call") is therefore structural — this module reaches no provider port at all,
which `test_v3_outfit_composition.py` asserts over the import graph.
"""

from __future__ import annotations

import uuid

from sqlalchemy import Select, select, union_all
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.application.purchase.candidate_normalizer import ParsedItem
from app.application.purchase.compute_similarity_evidence import load_wear_scenes
from app.application.purchase.outfit_composition import (
    CATEGORY_SLOTS,
    OutfitEvidence,
    build_outfit_evidence,
    unusable_candidate_evidence,
)
from app.application.purchase.wardrobe_similarity import (
    RELATION_COVERS,
    ClosetEvidence,
    OwnedItemFacts,
    normalize_style_tags,
)
from app.common.enums import FitStatus, ItemStatus
from app.infrastructure.persistence.models.wardrobe import WardrobeItem

# Upper bound on rows pulled into memory **per category** (see the module
# docstring). Smaller than V3-005's 200 because the jobs differ: V3-005 has to
# *count* how many similar items exist, so it needs a wide window; V3-006 only
# has to *pick* the best one or two per slot, and ranking quality saturates long
# before this. A smaller window also keeps the batched scene query small.
MAX_ITEMS_PER_CATEGORY = 60

# What "available" means: wearable right now. Deliberately narrower than
# `compute_similarity_evidence.OWNED_STATUSES` — see the module docstring.
AVAILABLE_STATUSES: tuple[str, ...] = (ItemStatus.WEARABLE.value,)


async def compose_outfit_evidence(
    session: AsyncSession,
    user_id: uuid.UUID,
    parsed: ParsedItem | None,
    closet_evidence: ClosetEvidence | None = None,
) -> OutfitEvidence:
    """Grounded outfit evidence for one purchase candidate.

    `closet_evidence` is V3-005's output. Its `covers` items seed the
    existing-item alternatives, so the two features interlock rather than each
    re-deciding what "similar" means. Passing `None` simply yields no
    alternatives — useful for a candidate whose redundancy was never computed.

    Never raises on a degraded candidate: a parse that yielded no category
    returns `unusable_candidate_evidence()`, because the decision flow has to
    keep working when parsing did not.
    """
    if parsed is None or not parsed.is_usable:
        return unusable_candidate_evidence()

    category = (parsed.category or "").strip().lower()
    if category not in CATEGORY_SLOTS:
        # Nothing to compose *around*, so there is nothing worth loading — the
        # pure layer refuses this input too (see `build_outfit_evidence`), and
        # checking here keeps an unplaceable candidate free of database cost.
        return unusable_candidate_evidence()
    similar_item_ids = _covering_item_ids(closet_evidence)
    categories = relevant_categories(category)

    items = await _load_available_items(session, user_id, categories, similar_item_ids)
    if not items:
        return build_outfit_evidence(parsed, [], similar_item_ids=similar_item_ids)

    scenes = await load_wear_scenes(session, [item.id for item in items])
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

    return build_outfit_evidence(
        parsed,
        facts,
        similar_item_ids=similar_item_ids,
        scan_truncated=_window_saturated(facts, categories),
    )


def _covering_item_ids(closet_evidence: ClosetEvidence | None) -> tuple[uuid.UUID, ...]:
    """V3-005's similar items that would do the candidate's job, in its order.

    Only `covers`: a `complements` item pairs with the candidate, so offering it
    as "you already own this instead" would be nonsense.
    """
    if closet_evidence is None:
        return ()
    return tuple(s.item_id for s in closet_evidence.similar_items if s.relation == RELATION_COVERS)


def relevant_categories(category: str) -> tuple[str, ...]:
    """Categories worth loading for a candidate of this category.

    A garment is irrelevant when every slot it occupies is already taken by the
    candidate: a dress cannot join an outfit whose legs slot is a pair of
    trousers under consideration. The candidate's own category is always
    included — that is where the existing-item alternatives come from.

    Ordered by `CATEGORY_SLOTS` so the scan's window list is stable, which keeps
    the compiled statement (and therefore the structural tests) deterministic.
    """
    prefilled = set(CATEGORY_SLOTS.get(category, ()))
    categories = [c for c, slots in CATEGORY_SLOTS.items() if not (set(slots) & prefilled)]
    if category in CATEGORY_SLOTS and category not in categories:
        categories.append(category)
    return tuple(categories)


def build_available_items_statement(
    user_id: uuid.UUID,
    categories: tuple[str, ...],
    pinned_ids: tuple[uuid.UUID, ...] = (),
) -> Select[tuple[WardrobeItem]]:
    """One independently bounded window per category, plus the pinned ids.

    Public so `tests/unit/test_v3_outfit_scan_window.py` can assert the
    availability predicates and the per-category bound by compiling the
    statement, which needs no container. Two invariants it pins:

    1. Every leg filters on `AVAILABLE_STATUSES`, `deleted_at IS NULL` and
       `fit_status != no_longer_fits`, so an unwearable item cannot reach an
       outfit however the ranking changes.
    2. Each category carries its own `LIMIT`, so no category can crowd another
       out of the scan and turn "I own shoes" into "you must buy shoes".

    `pinned_ids` are V3-005's similar items. They get their own window because
    they are chosen by attribute similarity, not recency, so an old-but-similar
    coat can rank top-5 for redundancy while sitting outside the recency window
    — and then acceptance 2's alternative would silently vanish for exactly the
    users who need it most. Pinning them still applies the availability filter,
    so a similar item in the laundry is loaded by neither leg.
    """

    def _available(stmt: Select[tuple[WardrobeItem]]) -> Select[tuple[WardrobeItem]]:
        return stmt.where(
            WardrobeItem.user_id == user_id,
            WardrobeItem.status.in_(AVAILABLE_STATUSES),
            WardrobeItem.deleted_at.is_(None),
            WardrobeItem.fit_status != FitStatus.NO_LONGER_FITS.value,
        )

    legs: list[Select[tuple[WardrobeItem]]] = [
        _available(select(WardrobeItem).where(WardrobeItem.category == category))
        .order_by(WardrobeItem.created_at.desc(), WardrobeItem.id)
        .limit(MAX_ITEMS_PER_CATEGORY)
        for category in categories
    ]
    if pinned_ids:
        legs.append(
            _available(select(WardrobeItem).where(WardrobeItem.id.in_(pinned_ids)))
            .order_by(WardrobeItem.created_at.desc(), WardrobeItem.id)
            .limit(len(pinned_ids))
        )
    if len(legs) == 1:
        return legs[0]
    scan = union_all(*legs).subquery()
    available = aliased(WardrobeItem, scan)
    return select(available).order_by(available.created_at.desc(), available.id)


async def _load_available_items(
    session: AsyncSession,
    user_id: uuid.UUID,
    categories: tuple[str, ...],
    pinned_ids: tuple[uuid.UUID, ...],
) -> list[WardrobeItem]:
    """Query 1. De-duplicated, because a pinned item is usually also in its
    category window and `UNION ALL` (unlike `UNION`) keeps both copies —
    `UNION ALL` is still the right operator, since deduplicating in Postgres
    would mean sorting the whole scan to drop a handful of rows."""
    rows = await session.scalars(build_available_items_statement(user_id, categories, pinned_ids))
    seen: set[uuid.UUID] = set()
    items: list[WardrobeItem] = []
    for item in rows:
        if item.id in seen:
            continue
        seen.add(item.id)
        items.append(item)
    return items


def _window_saturated(facts: list[OwnedItemFacts], categories: tuple[str, ...]) -> bool:
    """Did any category window fill up, i.e. is this pool a recent slice?

    Derived from rows already in hand, so it costs no extra query. Reported so
    a snapshot can say "there may be more alternatives" instead of implying the
    listed outfits are the only ones the wardrobe allows.
    """
    counts: dict[str, int] = {}
    for facts_row in facts:
        counts[facts_row.category] = counts.get(facts_row.category, 0) + 1
    return any(counts.get(category, 0) >= MAX_ITEMS_PER_CATEGORY for category in categories)


__all__ = [
    "AVAILABLE_STATUSES",
    "MAX_ITEMS_PER_CATEGORY",
    "build_available_items_statement",
    "compose_outfit_evidence",
    "relevant_categories",
]
