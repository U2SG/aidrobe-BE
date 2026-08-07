"""V3-007 — the decision use case against a real Postgres.

What only a database can prove, and is therefore here rather than in
`tests/unit/test_v3_purchase_decision_rules.py` (which owns the matrix itself):

- a real wardrobe produces the state the rules promise, end to end through
  V3-005 and V3-006;
- the decision is written once per candidate and a concurrent second writer
  loses on the unique index instead of creating a second explanation;
- the frozen snapshot keeps explaining itself after the wardrobe it described
  has been renamed, made unwearable and deleted — and a recomputation now would
  disagree, which is what makes that a real test rather than a tautology;
- the reasons name items that actually exist and are actually the caller's.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.purchase.decide_purchase import (
    DecidePurchase,
    parsed_item_from_snapshot,
    shown_decision,
)
from app.application.purchase.purchase_decision_rules import (
    ACTION_STYLE_WITH_EXISTING,
    CONCERN_VOLUME,
    REASON_EXTRA_PURCHASE_NEEDED,
    REASON_REDUNDANCY,
)
from app.application.purchase.wardrobe_similarity import RELATION_COVERS
from app.common.enums import (
    Category,
    DecisionVisualNeed,
    FitFeedbackType,
    FitScene,
    FitStatus,
    ItemStatus,
    PurchaseCandidateSource,
    PurchaseCandidateStatus,
    PurchaseDecision,
    WardrobeItemStatus,
)
from app.infrastructure.persistence.models.fit import FitFeedback
from app.infrastructure.persistence.models.purchase_decision import (
    PurchaseCandidate,
    WardrobeDecision,
)
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
) -> WardrobeItem:
    item = WardrobeItem(
        user_id=user_id,
        title=title,
        category=category,
        color_main=color,
        status=status,
        fit_status=fit_status,
    )
    session.add(item)
    await session.flush()
    return item


_DEFAULT_PARSED: dict[str, Any] = {
    "category": Category.OUTERWEAR.value,
    "colors": ["gray"],
    "silhouette": None,
    "material_hints": ["wool"],
    "price": "¥899",
    "scene_tags": [FitScene.COMMUTE.value],
}
# A distinct sentinel so `parsed=None` can mean "the column is NULL" (a
# `parse_failed` candidate) rather than "use the default".
_UNSET = object()


async def _add_candidate(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    parsed: Any = _UNSET,
    status: str = PurchaseCandidateStatus.PARSED.value,
) -> PurchaseCandidate:
    candidate = PurchaseCandidate(
        user_id=user_id,
        source_type=PurchaseCandidateSource.SHARE_LINK.value,
        source_url="https://example.com/gray-coat",
        status=status,
        parsed_item=dict(_DEFAULT_PARSED) if parsed is _UNSET else parsed,
    )
    session.add(candidate)
    await session.flush()
    return candidate


async def _rate(session: AsyncSession, user_id: uuid.UUID, item: WardrobeItem, scene: str) -> None:
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


async def _wearable_pool(session: AsyncSession, user_id: uuid.UUID) -> dict[str, WardrobeItem]:
    """Enough to complete an outfit around an outerwear candidate."""
    return {
        Category.TOP.value: await _add_item(
            session, user_id, category=Category.TOP.value, title="shirt"
        ),
        Category.BOTTOM.value: await _add_item(
            session, user_id, category=Category.BOTTOM.value, title="trousers"
        ),
        Category.SHOES.value: await _add_item(
            session, user_id, category=Category.SHOES.value, title="loafers"
        ),
    }


@contextmanager
def _count_sql() -> Iterator[list[str]]:
    statements: list[str] = []

    def _before(conn, cursor, statement, parameters, context, executemany) -> None:  # type: ignore[no-untyped-def]
        statements.append(statement)

    event.listen(Engine, "before_cursor_execute", _before)
    try:
        yield statements
    finally:
        event.remove(Engine, "before_cursor_execute", _before)


# --- Acceptance 1: a real wardrobe reaches the promised state ------------


async def test_a_wardrobe_that_can_wear_it_reaches_worth_adding(db_session: AsyncSession) -> None:
    user_id = await _seed_user(db_session)
    pool = await _wearable_pool(db_session, user_id)
    # Padding so the decision is grounded in a real wardrobe rather than three
    # items — below `MIN_GROUNDING_ITEMS` the rules deliberately stay unsure.
    for index in range(4):
        await _add_item(db_session, user_id, category=Category.TOP.value, title=f"tee-{index}")
    await _rate(db_session, user_id, pool[Category.TOP.value], FitScene.COMMUTE.value)
    candidate = await _add_candidate(db_session, user_id)

    decision = await DecidePurchase(db_session).execute(user_id, candidate)

    assert decision.decision == PurchaseDecision.WORTH_ADDING.value
    assert decision.candidate_id == candidate.id


async def test_owning_the_same_coat_in_the_same_colour_reaches_avoid_duplicate(
    db_session: AsyncSession,
) -> None:
    user_id = await _seed_user(db_session)
    await _wearable_pool(db_session, user_id)
    owned = await _add_item(
        db_session, user_id, category=Category.OUTERWEAR.value, title="gray coat", color="gray"
    )
    candidate = await _add_candidate(db_session, user_id)

    decision = await DecidePurchase(db_session).execute(user_id, candidate)
    shown = shown_decision(decision)

    assert decision.decision == PurchaseDecision.AVOID_DUPLICATE.value
    assert shown.reasons[0].kind == REASON_REDUNDANCY
    # The comparison names the coat that produced the verdict, not a guess.
    assert owned.id in {i.item_id for i in shown.closet_comparison.items}
    assert any(i.relation == RELATION_COVERS for i in shown.closet_comparison.items)
    # And it offers the wardrobe as the way out, because one can be worn.
    assert ACTION_STYLE_WITH_EXISTING in shown.next_actions


async def test_a_wardrobe_missing_a_required_slot_reaches_wait(db_session: AsyncSession) -> None:
    user_id = await _seed_user(db_session)
    await _add_item(db_session, user_id, category=Category.TOP.value, title="shirt")
    # No bottoms and no shoes: no complete look exists around the candidate.
    candidate = await _add_candidate(db_session, user_id)

    decision = await DecidePurchase(db_session).execute(user_id, candidate)
    shown = shown_decision(decision)

    assert decision.decision == PurchaseDecision.WAIT.value
    assert shown.reasons[0].kind == REASON_EXTRA_PURCHASE_NEEDED
    assert decision.visual_need == DecisionVisualNeed.NONE.value


async def test_a_failed_parse_is_decidable_rather_than_an_error(db_session: AsyncSession) -> None:
    """Contract §5.2: `parse_failed` still gets a decision — the honest one."""
    user_id = await _seed_user(db_session)
    await _wearable_pool(db_session, user_id)
    candidate = await _add_candidate(
        db_session, user_id, parsed=None, status=PurchaseCandidateStatus.PARSE_FAILED.value
    )

    decision = await DecidePurchase(db_session).execute(user_id, candidate)

    assert decision.decision == PurchaseDecision.INSUFFICIENT_INFORMATION.value
    assert decision.visual_need == DecisionVisualNeed.NONE.value
    assert decision.context_snapshot["parsed_item"] is None


async def test_only_the_callers_own_items_are_ever_compared(db_session: AsyncSession) -> None:
    """A stranger's identical coat must not appear in the comparison, and must
    not push the decision to `avoid_duplicate`."""
    user_id = await _seed_user(db_session)
    stranger_id = await _seed_user(db_session)
    await _wearable_pool(db_session, user_id)
    theirs = await _add_item(
        db_session, stranger_id, category=Category.OUTERWEAR.value, title="their coat", color="gray"
    )
    candidate = await _add_candidate(db_session, user_id)

    decision = await DecidePurchase(db_session).execute(user_id, candidate)
    shown = shown_decision(decision)

    assert theirs.id not in {i.item_id for i in shown.closet_comparison.items}
    assert decision.decision != PurchaseDecision.AVOID_DUPLICATE.value


async def test_an_unwearable_similar_item_still_counts_against_redundancy(
    db_session: AsyncSession,
) -> None:
    """The V3-005/V3-006 split, visible at the decision level: a coat in the
    laundry is still owned (so buying another IS a duplicate) but is never
    offered as something to wear."""
    user_id = await _seed_user(db_session)
    await _wearable_pool(db_session, user_id)
    dirty = await _add_item(
        db_session,
        user_id,
        category=Category.OUTERWEAR.value,
        title="gray coat",
        color="gray",
        status=WardrobeItemStatus.DIRTY.value,
    )
    candidate = await _add_candidate(db_session, user_id)

    decision = await DecidePurchase(db_session).execute(user_id, candidate)
    shown = shown_decision(decision)
    outfit_ids = {
        item_id
        for group in ("outfits", "alternatives")
        for outfit in decision.evidence_snapshot["outfit_evidence"][group]
        for item_id in outfit["item_ids"]
    }

    assert decision.decision == PurchaseDecision.AVOID_DUPLICATE.value
    assert dirty.id in {i.item_id for i in shown.closet_comparison.items}
    assert str(dirty.id) not in outfit_ids


# --- Idempotency (contract §5.3) -----------------------------------------


async def test_a_second_call_returns_the_first_decision(db_session: AsyncSession) -> None:
    user_id = await _seed_user(db_session)
    await _wearable_pool(db_session, user_id)
    candidate = await _add_candidate(db_session, user_id)

    first = await DecidePurchase(db_session).execute(user_id, candidate)
    second = await DecidePurchase(db_session).execute(user_id, candidate, concern=CONCERN_VOLUME)

    assert second.id == first.id
    # The second call's concern is deliberately ignored: re-deciding would let
    # the explanation change under a user who only asked again.
    assert second.context_snapshot["concern"] == "none"
    rows = list(
        await db_session.scalars(
            select(WardrobeDecision).where(WardrobeDecision.candidate_id == candidate.id)
        )
    )
    assert len(rows) == 1


async def test_a_concurrent_second_writer_loses_on_the_unique_index(
    db_session: AsyncSession,
) -> None:
    """The 23505 path, forced deterministically: a decision row already exists
    when the second writer flushes. The savepoint must contain the violation
    (a poisoned session would abort the whole request) and the loser must
    return the winner rather than raise.
    """
    user_id = await _seed_user(db_session)
    await _wearable_pool(db_session, user_id)
    candidate = await _add_candidate(db_session, user_id)
    winner = await DecidePurchase(db_session).execute(user_id, candidate)

    # A racing writer computed before the winner committed, so it never saw it.
    loser = WardrobeDecision(
        user_id=user_id,
        decision_type="purchase",
        candidate_id=candidate.id,
        context_snapshot={"concern": "none"},
        evidence_snapshot={"decision": {}},
        decision=PurchaseDecision.WAIT.value,
        uncertainty="high",
        visual_need="none",
    )
    returned = await DecidePurchase(db_session)._add_or_replay(loser, candidate.id, user_id)

    assert returned.id == winner.id
    # The session survived the violation: it can still be used.
    assert await db_session.scalar(select(WardrobeDecision.id).where(WardrobeDecision.id == winner.id))


# --- Acceptance 2/4: the snapshot is the explanation ---------------------


async def test_the_decision_replays_after_the_wardrobe_it_described_changed(
    db_session: AsyncSession,
) -> None:
    """V3-003's whole reason for existing, exercised by a real decision."""
    user_id = await _seed_user(db_session)
    pool = await _wearable_pool(db_session, user_id)
    owned = await _add_item(
        db_session, user_id, category=Category.OUTERWEAR.value, title="gray coat", color="gray"
    )
    candidate = await _add_candidate(db_session, user_id)
    decision = await DecidePurchase(db_session).execute(user_id, candidate)
    before = shown_decision(decision).as_payload()
    parsed_snapshot = dict(candidate.parsed_item or {})

    # The wardrobe moves on: renamed, unwearable, and one item gone entirely.
    owned.title = "renamed coat"
    pool[Category.SHOES.value].status = WardrobeItemStatus.DONATED.value
    await db_session.delete(pool[Category.BOTTOM.value])
    await db_session.flush()
    db_session.expunge_all()

    stored = await db_session.scalar(
        select(WardrobeDecision).where(WardrobeDecision.id == decision.id)
    )
    assert stored is not None

    assert shown_decision(stored).as_payload() == before
    # And a fresh evaluation of the same candidate would now say something
    # different — so the replay is doing real work.
    fresh = await _add_candidate(db_session, user_id, parsed=parsed_snapshot)
    recomputed = shown_decision(await DecidePurchase(db_session).execute(user_id, fresh))
    assert recomputed.as_payload() != before


async def test_the_snapshot_carries_both_evidence_payloads_and_the_shown_copy(
    db_session: AsyncSession,
) -> None:
    """The layout V3-008's `GET /v1/wardrobe-decisions/{id}` will read, and the
    key `tests/services/test_v3_outfit_evidence.py` already replays from."""
    user_id = await _seed_user(db_session)
    await _wearable_pool(db_session, user_id)
    candidate = await _add_candidate(db_session, user_id)

    decision = await DecidePurchase(db_session).execute(user_id, candidate)

    assert set(decision.evidence_snapshot) == {"closet_evidence", "outfit_evidence", "decision"}
    assert set(decision.context_snapshot) == {
        "concern",
        "parsed_item",
        "candidate_status",
        "source_type",
        "locale",
    }
    # Nothing signed or TTL-bound is ever frozen (V3_SPEC §13.5).
    assert "image_url" not in str(decision.evidence_snapshot)
    assert "http" not in str(decision.evidence_snapshot)
    # The columns agree with the copy the user was shown, so a query on
    # `decision`/`visual_need` cannot disagree with the explanation.
    shown = shown_decision(decision)
    assert (decision.decision, decision.uncertainty, decision.visual_need) == (
        shown.decision,
        shown.uncertainty,
        shown.visual_need,
    )


async def test_every_compared_item_id_is_a_real_item_of_this_user(
    db_session: AsyncSession,
) -> None:
    """Acceptance 2 at the database level: a reason may only cite evidence that
    exists. Item ids are the citable part."""
    user_id = await _seed_user(db_session)
    await _wearable_pool(db_session, user_id)
    await _add_item(
        db_session, user_id, category=Category.OUTERWEAR.value, title="gray coat", color="gray"
    )
    candidate = await _add_candidate(db_session, user_id)

    decision = await DecidePurchase(db_session).execute(user_id, candidate)
    cited = [i.item_id for i in shown_decision(decision).closet_comparison.items]

    assert cited
    owned = set(
        await db_session.scalars(select(WardrobeItem.id).where(WardrobeItem.user_id == user_id))
    )
    assert set(cited) <= owned


# --- Query budget --------------------------------------------------------


async def test_sql_count_does_not_grow_with_the_wardrobe(db_session: AsyncSession) -> None:
    """The N+1 guard against a real database — `tests/unit` counts session
    calls, this counts emitted statements."""
    user_id = await _seed_user(db_session)
    await _wearable_pool(db_session, user_id)
    small_candidate = await _add_candidate(db_session, user_id)
    with _count_sql() as small:
        await DecidePurchase(db_session).execute(user_id, small_candidate)

    for index in range(30):
        await _add_item(db_session, user_id, category=Category.TOP.value, title=f"tee-{index}")
    big_candidate = await _add_candidate(db_session, user_id)
    with _count_sql() as big:
        await DecidePurchase(db_session).execute(user_id, big_candidate)

    assert len(small) == len(big)


async def test_a_replayed_decision_does_not_touch_the_wardrobe(db_session: AsyncSession) -> None:
    user_id = await _seed_user(db_session)
    await _wearable_pool(db_session, user_id)
    candidate = await _add_candidate(db_session, user_id)
    await DecidePurchase(db_session).execute(user_id, candidate)

    with _count_sql() as statements:
        await DecidePurchase(db_session).execute(user_id, candidate)

    assert len(statements) == 1
    assert "wardrobe_items" not in statements[0]


# --- Snapshot reader tolerance -------------------------------------------


async def test_a_partial_parsed_item_is_read_without_raising(db_session: AsyncSession) -> None:
    """A degraded V3-004 parse writes whatever it recovered; the reader must
    accept it, including values of the wrong JSON type."""
    parsed = parsed_item_from_snapshot(
        {"category": Category.TOP.value, "colors": "gray", "scene_tags": [1, "commute"]}
    )

    assert parsed is not None
    assert parsed.category == Category.TOP.value
    assert parsed.colors is None
    assert parsed.scene_tags == ["commute"]
