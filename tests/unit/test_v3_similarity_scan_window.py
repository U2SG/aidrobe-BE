"""V3-005 — the scan window: same-category items are never displaceable.

The bug this module exists to prevent, in full: the first version of
`_load_items` applied one `LIMIT 200` across the candidate's category *and* its
complements. For `outerwear` the complement set is `{top, bottom, shoes}` —
effectively the whole wearable wardrobe — so a user whose 200 most recent items
in those four categories happened to contain no outerwear loaded **zero** coats,
and the pure layer then reported `redundancy_level="none"` with
`similar_item_count=0`. That is not a truncated answer, it is a confident wrong
one, and it errs toward encouraging the purchase.

Two properties are asserted here, both without a database:

1. *Structural* — the compiled statement gives the same-category window its own
   `LIMIT`, so no quantity of complements can push same-category rows out.
2. *Wiring* — a saturated same-category window reaches the payload as
   `same_category_scan_truncated`, so a floor is never emitted as a total.

The behavioral proof — seed an oversized complement-skewed wardrobe in Postgres
and assert the matching coat is still found — needs a real database and lives in
`tests/services/test_v3_similarity_evidence.py`, which CI runs.

Runs under `pytest --noconftest`.
"""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy.dialects import postgresql

from app.application.purchase import compute_similarity_evidence as scan
from app.application.purchase.candidate_normalizer import ParsedItem
from app.application.purchase.compute_similarity_evidence import (
    MAX_ITEMS_PER_CATEGORY_GROUP,
    build_wardrobe_scan_statement,
    compute_similarity_evidence,
)
from app.application.purchase.wardrobe_similarity import COMPLEMENTARY_CATEGORIES
from app.common.enums import Category, ItemStatus, WardrobeItemStatus
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from tests.unit.test_v3_similarity_query_count import RecordingSession

CANDIDATE_CATEGORY = Category.OUTERWEAR.value
COMPLEMENTS = COMPLEMENTARY_CATEGORIES[CANDIDATE_CATEGORY]


def _sql(category: str, complements: tuple[str, ...]) -> str:
    statement = build_wardrobe_scan_statement(uuid.uuid4(), category, complements)
    return str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    )


# --- 1. Structural: the per-group bound ---------------------------------


def test_same_category_window_is_bounded_independently_of_complements() -> None:
    """Two `LIMIT`s, one per category group — the invariant, in the SQL.

    Asserted on the compiled statement rather than on a result set because this
    is a property of the *query*: one `LIMIT` shared across both groups is the
    displacement bug, and no fixture size can prove its absence.
    """
    sql = _sql(CANDIDATE_CATEGORY, COMPLEMENTS)

    assert sql.count("LIMIT") == 2, f"expected one LIMIT per category group:\n{sql}"
    assert sql.count(f"LIMIT {MAX_ITEMS_PER_CATEGORY_GROUP}") == 2
    assert "UNION ALL" in sql
    # Each group is filtered in its own leg, so neither shares the other's rows.
    assert f"IN ('{CANDIDATE_CATEGORY}')" in sql
    assert ", ".join(f"'{c}'" for c in COMPLEMENTS) in sql


def test_a_candidate_without_complements_still_gets_a_bounded_window() -> None:
    """No complements means nothing can displace the candidate's category, so
    the single window already *is* the per-group window — and it is still
    bounded, rather than an unbounded scan."""
    sql = _sql("accessory", ())

    assert "UNION ALL" not in sql
    assert sql.count(f"LIMIT {MAX_ITEMS_PER_CATEGORY_GROUP}") == 1


def test_the_scan_is_ordered_outside_the_union() -> None:
    """Postgres does not carry a leg's `ORDER BY` through `UNION ALL`.

    Without an outer ordering the scan order would be arbitrary, and
    `order_by_ids` appends its unranked tail in scan order — so the pgvector
    path would rank the same wardrobe differently run to run, breaking
    acceptance 1's determinism and making V3-007's snapshots unreproducible.
    """
    sql = _sql(CANDIDATE_CATEGORY, COMPLEMENTS)

    outer_order_by = sql.rsplit(") AS ", 1)[-1]
    assert "ORDER BY" in outer_order_by, f"union result is unordered:\n{sql}"
    assert "created_at DESC" in outer_order_by
    # Tie-broken on the id: `created_at` has a `server_default=func.now()`, so
    # items written in one transaction share a timestamp exactly.
    assert outer_order_by.rstrip().endswith(".id")


def test_owned_statuses_include_the_laundry_basket_but_not_disposal() -> None:
    """A dirty coat is still a coat you own; a donated one is not.

    `wearable`-only would understate ownership for a *redundancy* verdict — the
    buy-encouraging direction — while `lost`/`donated` genuinely are not owned.
    """
    sql = _sql(CANDIDATE_CATEGORY, COMPLEMENTS)

    assert ItemStatus.WEARABLE.value in scan.OWNED_STATUSES
    assert WardrobeItemStatus.DIRTY.value in scan.OWNED_STATUSES
    for gone in (WardrobeItemStatus.LOST, WardrobeItemStatus.DONATED, ItemStatus.UNAVAILABLE):
        assert gone.value not in scan.OWNED_STATUSES
    assert "IN ('wearable', 'dirty')" in sql


# --- 2. Wiring: a saturated window is declared, not hidden --------------


def _item(*, category: str = CANDIDATE_CATEGORY) -> WardrobeItem:
    return WardrobeItem(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        title="coat",
        category=category,
        color_main="gray",
        status=ItemStatus.WEARABLE.value,
    )


def _candidate() -> ParsedItem:
    return ParsedItem(category=CANDIDATE_CATEGORY, colors=["gray"], scene_tags=["commute"])


async def test_a_full_same_category_window_is_reported_as_truncated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Window saturated → the count is a floor and the payload says so."""
    monkeypatch.setattr(scan, "MAX_ITEMS_PER_CATEGORY_GROUP", 3)
    session = RecordingSession([_item() for _ in range(3)])

    evidence = await compute_similarity_evidence(session, uuid.uuid4(), _candidate())

    assert evidence.same_category_scan_truncated is True
    assert evidence.similar_item_count == 3


async def test_an_unsaturated_window_reports_a_true_total(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The common case: the scan saw everything, so the count is exact."""
    monkeypatch.setattr(scan, "MAX_ITEMS_PER_CATEGORY_GROUP", 10)
    session = RecordingSession([_item() for _ in range(4)])

    evidence = await compute_similarity_evidence(session, uuid.uuid4(), _candidate())

    assert evidence.same_category_scan_truncated is False
    assert evidence.similar_item_count == 4


async def test_complements_filling_the_scan_do_not_mark_the_count_a_floor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The flag is scoped to the same-category window.

    A wardrobe full of trousers says nothing about how completely the coats were
    counted; marking that count as a floor would be a false caveat on a
    complete answer.
    """
    monkeypatch.setattr(scan, "MAX_ITEMS_PER_CATEGORY_GROUP", 3)
    items = [_item()] + [_item(category=Category.BOTTOM.value) for _ in range(3)]
    session = RecordingSession(items)

    evidence = await compute_similarity_evidence(session, uuid.uuid4(), _candidate())

    assert evidence.same_category_scan_truncated is False
    assert evidence.similar_item_count == 1
