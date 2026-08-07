"""V3-006 — the availability scan: what may reach an outfit, and how much.

Acceptance 1 says every suggested supporting item must belong to the user *and
be available*. That is enforced in the WHERE clause, so it is asserted here on
the compiled statement: a services test that seeds a dirty item and finds it
absent would also pass if the scan had simply not reached that far, whereas a
missing predicate fails this file immediately.

Two properties, both without a database:

1. *Structural* — every leg filters on `AVAILABLE_STATUSES`, `deleted_at IS
   NULL` and `fit_status != no_longer_fits`, and each category carries its own
   `LIMIT`. One `LIMIT` shared across categories is the V3-005 displacement bug
   in a costlier form: 60 recent tops would hide every pair of shoes and the
   evidence would claim the user must *buy* shoes they already own.
2. *Wiring* — a saturated window reaches the payload as `scan_truncated`, so a
   sampled pool is never presented as the whole wardrobe.

The behavioral proof — seed unwearable items in Postgres and assert no outfit
names them — needs a real database and lives in
`tests/services/test_v3_outfit_evidence.py`, which CI runs.

Same caveat as V3-005's scan-window file: the structural assertions read
*compiled SQL text*, so they are deliberately coupled to how SQLAlchemy renders
aliases, `LIMIT` and bound parameters. A red here after a dependency bump means
the rendering changed, not that the bound regressed.

Runs under `pytest --noconftest`.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.dialects import postgresql

from app.application.purchase import compose_outfit_evidence as scan
from app.application.purchase.candidate_normalizer import ParsedItem
from app.application.purchase.compose_outfit_evidence import (
    AVAILABLE_STATUSES,
    MAX_ITEMS_PER_CATEGORY,
    build_available_items_statement,
    compose_outfit_evidence,
    relevant_categories,
)
from app.application.purchase.compute_similarity_evidence import OWNED_STATUSES
from app.common.enums import Category, FitStatus, ItemStatus, WardrobeItemStatus
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from tests.unit.test_v3_similarity_query_count import RecordingSession

CANDIDATE_CATEGORY = Category.TOP.value


def _sql(category: str = CANDIDATE_CATEGORY, pinned: tuple[uuid.UUID, ...] = ()) -> str:
    statement = build_available_items_statement(
        uuid.uuid4(), relevant_categories(category), pinned
    )
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


# --- 1. Structural: availability is a predicate, not a convention -------


def test_every_leg_filters_to_items_the_user_can_actually_wear() -> None:
    sql = _sql()
    legs = len(relevant_categories(CANDIDATE_CATEGORY))

    assert sql.count("IN ('wearable')") == legs
    assert sql.count("deleted_at IS NULL") == legs
    assert sql.count("fit_status != 'no_longer_fits'") == legs


def test_available_is_deliberately_narrower_than_v3_005_owned() -> None:
    """The two features ask different questions, so they must not share a set.

    V3-005 asks "do I already own one of these", where a coat in the laundry
    counts. V3-006 asks "can this be worn tonight", where it does not — offering
    a dirty coat as a supporting item is a suggestion the user cannot act on.
    """
    assert AVAILABLE_STATUSES == (ItemStatus.WEARABLE.value,)
    assert WardrobeItemStatus.DIRTY.value in OWNED_STATUSES
    assert WardrobeItemStatus.DIRTY.value not in AVAILABLE_STATUSES
    assert FitStatus.NO_LONGER_FITS.value in _sql()


def test_each_category_is_bounded_independently() -> None:
    """One `LIMIT` per category: no category can crowd another out of the scan,
    so a full wardrobe of tops can never render as "you must buy shoes"."""
    sql = _sql()
    categories = relevant_categories(CANDIDATE_CATEGORY)

    assert sql.count(f"LIMIT {MAX_ITEMS_PER_CATEGORY}") == len(categories)
    assert sql.count("UNION ALL") == len(categories) - 1
    for category in categories:
        assert f"category = '{category}'" in sql


def test_the_scan_is_ordered_outside_the_union() -> None:
    """Postgres does not carry a leg's `ORDER BY` through `UNION ALL`, and the
    composition layer treats input order as its tie-break — so an unordered
    union would let the same wardrobe compose two different outfits."""
    sql = _sql()

    outer_order_by = sql.rsplit(") AS ", 1)[-1]
    assert "ORDER BY" in outer_order_by, f"union result is unordered:\n{sql}"
    assert "created_at DESC" in outer_order_by
    assert outer_order_by.rstrip().endswith(".id")


def test_similar_items_are_pinned_into_the_scan_with_the_same_filter() -> None:
    """V3-005 ranks by attribute similarity, not recency, so an old-but-similar
    coat can be top-ranked for redundancy while sitting outside every recency
    window — and acceptance 2's alternative would vanish for exactly the users
    who need it. Pinning it still applies the availability filter."""
    pinned = (uuid.UUID(int=7), uuid.UUID(int=8))
    sql = _sql(pinned=pinned)
    legs = len(relevant_categories(CANDIDATE_CATEGORY)) + 1

    assert sql.count("IN ('wearable')") == legs
    assert sql.count("fit_status != 'no_longer_fits'") == legs
    assert "id IN ('00000000-0000-0000-0000-000000000007'" in sql


def test_irrelevant_categories_are_never_loaded() -> None:
    """A dress cannot join an outfit whose legs slot is the candidate itself, so
    loading dresses for a trousers candidate would be paid-for noise."""
    categories = relevant_categories(Category.BOTTOM.value)

    assert Category.DRESS.value not in categories
    assert set(categories) == {
        Category.TOP.value,
        Category.OUTERWEAR.value,
        Category.SHOES.value,
        Category.BOTTOM.value,
    }
    assert f"category = '{Category.DRESS.value}'" not in _sql(Category.BOTTOM.value)


def test_a_dress_candidate_still_loads_its_own_category_for_alternatives() -> None:
    """Its slots are all prefilled, so only the alternatives need it — and
    acceptance 2 is exactly the case where they do."""
    categories = relevant_categories(Category.DRESS.value)

    assert set(categories) == {
        Category.OUTERWEAR.value,
        Category.SHOES.value,
        Category.DRESS.value,
    }


# --- 2. Wiring: a saturated window is declared, not hidden --------------


def _item(*, category: str = Category.BOTTOM.value) -> WardrobeItem:
    return WardrobeItem(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        title="trousers",
        category=category,
        color_main="black",
        status=ItemStatus.WEARABLE.value,
    )


def _candidate() -> ParsedItem:
    return ParsedItem(category=CANDIDATE_CATEGORY, colors=["gray"], scene_tags=["commute"])


@pytest.mark.parametrize(
    ("limit", "loaded", "truncated"),
    [(4, 3, False), (3, 3, True)],
)
async def test_the_saturation_boundary_is_exact(
    monkeypatch: pytest.MonkeyPatch,
    limit: int,
    loaded: int,
    truncated: bool,
) -> None:
    """One item below the window is a complete pool; filling it is a sample.

    The pair is the point: an off-by-one would either caveat a wardrobe that was
    scanned completely, or present a sampled pool as the whole thing.
    """
    monkeypatch.setattr(scan, "MAX_ITEMS_PER_CATEGORY", limit)
    session = RecordingSession([_item() for _ in range(loaded)])

    evidence = await compose_outfit_evidence(session, uuid.uuid4(), _candidate())

    assert evidence.scan_truncated is truncated


async def test_an_irrelevant_category_filling_up_does_not_mark_the_pool_sampled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flag is scoped to the categories this candidate actually composes
    with; a wardrobe full of dresses says nothing about the trousers pool."""
    monkeypatch.setattr(scan, "MAX_ITEMS_PER_CATEGORY", 2)
    items = [_item(category=Category.DRESS.value) for _ in range(2)]
    session = RecordingSession(items)

    evidence = await compose_outfit_evidence(session, uuid.uuid4(), _candidate())

    assert evidence.scan_truncated is False
