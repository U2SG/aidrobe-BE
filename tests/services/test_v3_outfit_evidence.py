"""V3-006 — grounded outfit evidence against a real Postgres.

What only a database can prove, and is therefore here rather than in the three
`tests/unit` modules: that the availability filter really filters (another
user's items, soft-deleted, dirty, no-longer-fitting), that a per-category
window cannot be crowded out by a wardrobe skewed to one category, that
`fit_feedback` joins to the right item, and — acceptance 4 — that a snapshot
written to `wardrobe_decisions` keeps explaining itself after the wardrobe it
described has been renamed, made unwearable and deleted.

The composition *rules* are fixtures in `tests/unit/test_v3_outfit_composition.py`
and the SQL predicates are compiled-statement assertions in
`tests/unit/test_v3_outfit_scan_window.py`; those run without a container, so
they are local evidence too. This file is the behavioral half CI runs.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.purchase import compose_outfit_evidence as compose_module
from app.application.purchase.candidate_normalizer import ParsedItem
from app.application.purchase.compose_outfit_evidence import compose_outfit_evidence
from app.application.purchase.compute_similarity_evidence import compute_similarity_evidence
from app.application.purchase.outfit_composition import (
    OUTFIT_STATUS_COMPOSED,
    OUTFIT_STATUS_INCOMPLETE,
    OUTFIT_STATUS_NO_AVAILABLE_ITEMS,
    REASON_SHARED_SCENE,
    SLOT_FEET,
    SLOT_LEGS,
    OutfitEvidence,
    replay_outfit_evidence,
)
from app.application.purchase.wardrobe_similarity import (
    REDUNDANCY_HIGH,
    SCENE_SOURCE_WEAR_FEEDBACK,
)
from app.common.enums import (
    Category,
    DecisionUncertainty,
    DecisionVisualNeed,
    FitFeedbackType,
    FitScene,
    FitStatus,
    ItemStatus,
    PurchaseDecision,
    WardrobeItemStatus,
)
from app.infrastructure.persistence.models.fit import FitFeedback
from app.infrastructure.persistence.models.purchase_decision import WardrobeDecision
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem


async def _seed_user(session: AsyncSession) -> uuid.UUID:
    user_id = uuid.uuid4()
    session.add(User(id=user_id))
    await session.flush()
    return user_id


async def _add_item(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    category: str,
    title: str = "item",
    color: str = "black",
    status: str = ItemStatus.WEARABLE.value,
    fit_status: str = FitStatus.UNKNOWN.value,
    style_tags: object = None,
    deleted: bool = False,
    created_at: datetime | None = None,
) -> WardrobeItem:
    # `created_at` is explicit wherever the scan's `ORDER BY created_at DESC`
    # decides the outcome: the column is `server_default=func.now()` and
    # Postgres' `now()` is transaction-scoped, so rows seeded by one test
    # otherwise share a single timestamp and the ordering would be arbitrary.
    item = WardrobeItem(
        user_id=user_id,
        title=title,
        category=category,
        color_main=color,
        status=status,
        fit_status=fit_status,
        style_tags=style_tags,
        deleted_at=datetime.now(UTC) if deleted else None,
        **({"created_at": created_at} if created_at is not None else {}),
    )
    session.add(item)
    await session.flush()
    return item


async def _wearable_outfit_pool(
    session: AsyncSession, user_id: uuid.UUID
) -> dict[str, WardrobeItem]:
    """One wearable item per slot the candidate does not fill."""
    return {
        Category.BOTTOM.value: await _add_item(
            session, user_id, category=Category.BOTTOM.value, title="trousers"
        ),
        Category.SHOES.value: await _add_item(
            session, user_id, category=Category.SHOES.value, title="loafers"
        ),
    }


async def _rate_scene(
    session: AsyncSession, user_id: uuid.UUID, item: WardrobeItem, scene: str
) -> None:
    session.add(
        FitFeedback(
            user_id=user_id,
            item_id=item.id,
            feedback_type=FitFeedbackType.FITS_WELL.value,
            scene=scene,
            created_at=datetime.now(UTC),
        )
    )
    await session.flush()


def _candidate(**overrides: Any) -> ParsedItem:
    base: dict[str, Any] = {
        "category": Category.TOP.value,
        "colors": ["gray"],
        "scene_tags": [FitScene.COMMUTE.value],
    }
    base.update(overrides)
    return ParsedItem(**base)


def _suggested_ids(evidence: OutfitEvidence) -> set[uuid.UUID]:
    """Every item the evidence asks the user to wear — outfits and alternatives.

    Acceptance 1 is about *every* suggested supporting item, so the grounding
    assertions check this union rather than the first outfit.
    """
    ids: set[uuid.UUID] = set()
    for outfit in (*evidence.outfits, *evidence.alternatives):
        ids.update(outfit.item_ids)
    return ids


@contextmanager
def _count_sql() -> Iterator[list[str]]:
    """Every statement the block emits, in order."""
    statements: list[str] = []

    def _before(conn, cursor, statement, parameters, context, executemany) -> None:  # type: ignore[no-untyped-def]
        statements.append(statement)

    event.listen(Engine, "before_cursor_execute", _before)
    try:
        yield statements
    finally:
        event.remove(Engine, "before_cursor_execute", _before)


# --- Acceptance 1: grounded in the caller's available wardrobe ----------


async def test_only_the_callers_available_items_reach_an_outfit(
    db_session: AsyncSession,
) -> None:
    """Every exclusion the filter promises, seeded at once.

    Each decoy is a *bottom* — the slot the outfit must fill — so if any of
    them leaked through it would be picked rather than merely ignored.
    """
    user_id = await _seed_user(db_session)
    stranger_id = await _seed_user(db_session)
    wearable = await _wearable_outfit_pool(db_session, user_id)

    decoys = [
        await _add_item(db_session, stranger_id, category=Category.BOTTOM.value, title="theirs"),
        await _add_item(
            db_session,
            user_id,
            category=Category.BOTTOM.value,
            title="laundry",
            status=WardrobeItemStatus.DIRTY.value,
        ),
        await _add_item(
            db_session,
            user_id,
            category=Category.BOTTOM.value,
            title="lost",
            status=WardrobeItemStatus.LOST.value,
        ),
        await _add_item(
            db_session,
            user_id,
            category=Category.BOTTOM.value,
            title="outgrown",
            fit_status=FitStatus.NO_LONGER_FITS.value,
        ),
        await _add_item(
            db_session, user_id, category=Category.BOTTOM.value, title="binned", deleted=True
        ),
    ]

    evidence = await compose_outfit_evidence(db_session, user_id, _candidate())

    assert evidence.status == OUTFIT_STATUS_COMPOSED
    assert _suggested_ids(evidence) == {item.id for item in wearable.values()}
    for decoy in decoys:
        assert decoy.id not in _suggested_ids(evidence)


async def test_a_dirty_item_is_owned_for_redundancy_but_never_suggested_to_wear(
    db_session: AsyncSession,
) -> None:
    """The two features disagree about the laundry basket, on purpose.

    V3-005 counts a dirty coat as owned (it is), V3-006 refuses to build an
    outfit around it (you cannot wear it tonight). Asserting both in one test
    pins the distinction, so widening either status set fails here.
    """
    user_id = await _seed_user(db_session)
    await _wearable_outfit_pool(db_session, user_id)
    dirty_top = await _add_item(
        db_session,
        user_id,
        category=Category.TOP.value,
        title="grey tee in the wash",
        color="gray",
        status=WardrobeItemStatus.DIRTY.value,
    )

    closet = await compute_similarity_evidence(db_session, user_id, _candidate())
    evidence = await compose_outfit_evidence(db_session, user_id, _candidate(), closet)

    assert closet.redundancy_level == REDUNDANCY_HIGH
    assert dirty_top.id in {s.item_id for s in closet.similar_items}
    assert evidence.alternatives == ()
    assert dirty_top.id not in _suggested_ids(evidence)


async def test_an_empty_wardrobe_says_so_instead_of_composing(
    db_session: AsyncSession,
) -> None:
    user_id = await _seed_user(db_session)

    evidence = await compose_outfit_evidence(db_session, user_id, _candidate())

    assert evidence.status == OUTFIT_STATUS_NO_AVAILABLE_ITEMS
    assert evidence.outfits == ()


async def test_a_missing_slot_is_reported_rather_than_filled_with_a_guess(
    db_session: AsyncSession,
) -> None:
    user_id = await _seed_user(db_session)
    await _add_item(db_session, user_id, category=Category.BOTTOM.value)

    evidence = await compose_outfit_evidence(db_session, user_id, _candidate())

    assert evidence.status == OUTFIT_STATUS_INCOMPLETE
    assert evidence.missing_slots == (SLOT_FEET,)
    assert evidence.missing_categories == (Category.SHOES.value,)


# --- Acceptance 2: an existing-item alternative -------------------------


async def test_a_redundant_candidate_yields_an_owned_alternative_outfit(
    db_session: AsyncSession,
) -> None:
    user_id = await _seed_user(db_session)
    pool = await _wearable_outfit_pool(db_session, user_id)
    owned_top = await _add_item(
        db_session, user_id, category=Category.TOP.value, title="grey tee", color="gray"
    )

    closet = await compute_similarity_evidence(db_session, user_id, _candidate())
    evidence = await compose_outfit_evidence(db_session, user_id, _candidate(), closet)

    assert closet.redundancy_level == REDUNDANCY_HIGH
    assert len(evidence.alternatives) == 1
    alternative = evidence.alternatives[0]
    assert alternative.anchor_item_id == owned_top.id
    # A complete look built only from things the user already has.
    assert set(alternative.item_ids) == {owned_top.id, *(i.id for i in pool.values())}


async def test_an_old_but_similar_item_is_pinned_into_the_scan(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """V3-005 ranks by attribute similarity, V3-006's windows by recency.

    Without the pinned leg the alternative would silently disappear for exactly
    the users who own the most clothes — the ones a redundancy warning matters
    most to. Timestamps are explicit because `created_at` is transaction-scoped.
    """
    monkeypatch.setattr(compose_module, "MAX_ITEMS_PER_CATEGORY", 2)
    user_id = await _seed_user(db_session)
    now = datetime.now(UTC)
    await _wearable_outfit_pool(db_session, user_id)
    old_match = await _add_item(
        db_session,
        user_id,
        category=Category.TOP.value,
        title="grey tee",
        color="gray",
        created_at=now - timedelta(days=400),
    )
    for index in range(3):
        await _add_item(
            db_session,
            user_id,
            category=Category.TOP.value,
            title=f"newer top {index}",
            color="navy",
            created_at=now - timedelta(days=index),
        )

    closet = await compute_similarity_evidence(db_session, user_id, _candidate())
    evidence = await compose_outfit_evidence(db_session, user_id, _candidate(), closet)

    assert [a.anchor_item_id for a in evidence.alternatives][:1] == [old_match.id]
    assert evidence.scan_truncated is True


async def test_a_top_heavy_wardrobe_cannot_hide_the_shoes(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The per-category bound, against a real database.

    One `LIMIT` shared across categories would let the newest tops fill the
    whole scan, leaving no shoes loaded — and the evidence would then tell a
    user who owns loafers that they must buy shoes. The window is shrunk rather
    than seeding hundreds of rows: the bug is about the window being *shared*,
    not about its size.
    """
    monkeypatch.setattr(compose_module, "MAX_ITEMS_PER_CATEGORY", 2)
    user_id = await _seed_user(db_session)
    now = datetime.now(UTC)
    shoes = await _add_item(
        db_session,
        user_id,
        category=Category.SHOES.value,
        title="loafers",
        created_at=now - timedelta(days=90),
    )
    await _add_item(
        db_session,
        user_id,
        category=Category.BOTTOM.value,
        created_at=now - timedelta(days=89),
    )
    for index in range(5):
        await _add_item(
            db_session,
            user_id,
            category=Category.TOP.value,
            title=f"new top {index}",
            created_at=now - timedelta(hours=index),
        )

    evidence = await compose_outfit_evidence(db_session, user_id, _candidate())

    assert evidence.missing_categories == ()
    assert evidence.status == OUTFIT_STATUS_COMPOSED
    assert shoes.id in _suggested_ids(evidence)


# --- Scene grounding ----------------------------------------------------


async def test_wear_feedback_grounds_the_outfit_scene(db_session: AsyncSession) -> None:
    user_id = await _seed_user(db_session)
    await _add_item(db_session, user_id, category=Category.SHOES.value)
    plain = await _add_item(db_session, user_id, category=Category.BOTTOM.value, title="plain")
    commuted = await _add_item(
        db_session, user_id, category=Category.BOTTOM.value, title="commute trousers"
    )
    await _rate_scene(db_session, user_id, commuted, FitScene.COMMUTE.value)

    outfit = (await compose_outfit_evidence(db_session, user_id, _candidate())).outfits[0]

    legs = next(s for s in outfit.slots if s.slot == SLOT_LEGS)
    assert legs.item_id == commuted.id
    assert plain.id != legs.item_id
    assert (outfit.scene, outfit.scene_source) == (
        FitScene.COMMUTE.value,
        SCENE_SOURCE_WEAR_FEEDBACK,
    )
    assert REASON_SHARED_SCENE in outfit.match_reasons


# --- Acceptance 4: reproducible from the stored snapshot ----------------


async def test_the_snapshot_replays_after_the_wardrobe_it_described_changed(
    db_session: AsyncSession,
) -> None:
    """The property acceptance 4 actually asks for.

    A round-trip through the dataclasses proves only that the reader matches the
    writer. What matters is that the explanation survives its subject: the items
    the outfit named are renamed, made unwearable and hard-deleted, and the
    stored snapshot still replays byte-identically. It can, because the evidence
    references items *inside* JSONB rather than through a join — the V3-003
    design, exercised here for the first time by a real payload.
    """
    user_id = await _seed_user(db_session)
    pool = await _wearable_outfit_pool(db_session, user_id)
    owned_top = await _add_item(
        db_session, user_id, category=Category.TOP.value, title="grey tee", color="gray"
    )
    closet = await compute_similarity_evidence(db_session, user_id, _candidate())
    evidence = await compose_outfit_evidence(db_session, user_id, _candidate(), closet)
    payload = evidence.as_payload()
    assert evidence.outfits and evidence.alternatives, "the snapshot must have something to prove"

    decision = WardrobeDecision(
        user_id=user_id,
        decision_type="purchase",
        context_snapshot={"parsed_item": _candidate().as_payload()},
        evidence_snapshot={"outfit_evidence": payload},
        decision=PurchaseDecision.CONSIDER_ALTERNATE.value,
        uncertainty=DecisionUncertainty.LOW.value,
        visual_need=DecisionVisualNeed.NONE.value,
    )
    db_session.add(decision)
    await db_session.flush()
    decision_id = decision.id

    # The wardrobe moves on: renamed, unwearable, and one item gone entirely.
    pool[Category.BOTTOM.value].title = "renamed trousers"
    pool[Category.SHOES.value].status = WardrobeItemStatus.DONATED.value
    await db_session.delete(owned_top)
    await db_session.flush()
    db_session.expunge_all()

    stored = await db_session.scalar(
        select(WardrobeDecision).where(WardrobeDecision.id == decision_id)
    )
    assert stored is not None
    replayed = replay_outfit_evidence(stored.evidence_snapshot["outfit_evidence"])

    assert replayed.as_payload() == payload
    # And recomputing now would NOT agree — which is the whole point of storing it.
    recomputed = await compose_outfit_evidence(db_session, user_id, _candidate())
    assert recomputed.as_payload() != payload


# --- Query budget -------------------------------------------------------


async def test_sql_count_does_not_grow_with_the_wardrobe(db_session: AsyncSession) -> None:
    """The N+1 guard against a real database.

    Asserted as "constant, and small" rather than as one magic number: the exact
    figure also depends on connection-level statements the driver may emit,
    while the property V3-006 commits to is that a bigger wardrobe does not cost
    more round-trips. The exact per-call budget is pinned in
    `tests/unit/test_v3_outfit_query_count.py`.
    """
    user_id = await _seed_user(db_session)
    await _wearable_outfit_pool(db_session, user_id)

    with _count_sql() as small:
        await compose_outfit_evidence(db_session, user_id, _candidate())

    for index in range(40):
        await _add_item(db_session, user_id, category=Category.BOTTOM.value, title=f"b{index}")

    with _count_sql() as large:
        evidence = await compose_outfit_evidence(db_session, user_id, _candidate())

    assert len(small) == len(large), f"query count grew: {len(small)} -> {len(large)}"
    assert len(large) <= 3, f"expected a bounded budget, emitted {len(large)}: {large}"
    assert evidence.status == OUTFIT_STATUS_COMPOSED
