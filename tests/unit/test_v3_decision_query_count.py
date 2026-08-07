"""V3-007 — the decision's query budget, counted exactly, without a database.

Same guard as V3-005's and V3-006's equivalents: a decision must cost a
*constant* number of round-trips, because it composes two evidence modules that
each already resisted an N+1 and it would be easy to reintroduce one between
them. The session double counts the calls and runs anywhere, so this stays
local evidence as well as CI evidence;
`tests/services/test_v3_purchase_decision.py` re-checks the same invariant
against real emitted SQL.

The idempotent path matters as much as the budget: a repeated POST must not
re-scan the wardrobe at all.

Runs under `pytest --noconftest`.
"""

from __future__ import annotations

import uuid
from types import TracebackType
from typing import Any

import pytest

from app.application.purchase.decide_purchase import DecidePurchase
from app.common.enums import Category, ItemStatus, PurchaseCandidateStatus
from app.infrastructure.persistence.models.purchase_decision import (
    PurchaseCandidate,
    WardrobeDecision,
)
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from tests.unit.test_v3_similarity_query_count import RecordingSession

# 1 existing-decision read + 1 locale + 2 similarity + 2 outfit composition,
# then the insert's flush. No vector tier: `PGVECTOR_ENABLED` is off by default.
EXPECTED_READS = 6
EXPECTED_CALLS = EXPECTED_READS + 1  # + flush


class _Savepoint:
    async def __aenter__(self) -> _Savepoint:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        return None


class DecisionSession(RecordingSession):
    """`RecordingSession` plus the writes a decision performs.

    `scalar` is scripted in call order rather than returning one canned value,
    because the decision makes three different scalar reads (existing decision,
    locale, and — only when pgvector is on — the embedding anchor) and a double
    that answered them all identically would hide a miscount.
    """

    def __init__(self, items: list[WardrobeItem], *, existing: WardrobeDecision | None = None) -> None:
        super().__init__(items)
        self.scalar_results: list[Any] = [existing, None]
        self.added: list[Any] = []

    async def scalars(self, _statement: Any) -> list[Any]:
        """Always the item rows.

        The parent returns ids on its second `scalars` call, which is V3-005's
        embedding-ranking shape; here the second one is V3-006's availability
        scan, which wants rows. With `PGVECTOR_ENABLED` off — the default this
        test asserts the budget for — the vector call never happens.
        """
        self.calls.append("scalars")
        return list(self.items)

    async def scalar(self, _statement: Any) -> Any:
        self.calls.append("scalar")
        if self.scalar_results:
            return self.scalar_results.pop(0)
        return None

    def add(self, instance: Any) -> None:
        self.added.append(instance)

    async def flush(self) -> None:
        self.calls.append("flush")

    def begin_nested(self) -> _Savepoint:
        return _Savepoint()


def _item(*, category: str = Category.BOTTOM.value) -> WardrobeItem:
    return WardrobeItem(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        title="trousers",
        category=category,
        color_main="black",
        status=ItemStatus.WEARABLE.value,
    )


def _wardrobe(size: int) -> list[WardrobeItem]:
    items = [_item(), _item(category=Category.SHOES.value)]
    items.extend(_item() for _ in range(max(0, size - len(items))))
    return items


def _candidate(*, parsed: dict[str, Any] | None = None) -> PurchaseCandidate:
    return PurchaseCandidate(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        source_type="share_link",
        source_url="https://example.com/coat",
        status=PurchaseCandidateStatus.PARSED.value,
        parsed_item={"category": Category.TOP.value, "colors": ["gray"]} if parsed is None else parsed,
    )


@pytest.mark.parametrize("wardrobe_size", [2, 10, 300])
async def test_query_count_is_constant_in_wardrobe_size(wardrobe_size: int) -> None:
    session = DecisionSession(_wardrobe(wardrobe_size))

    decision = await DecidePurchase(session).execute(uuid.uuid4(), _candidate())  # type: ignore[arg-type]

    assert session.calls.count("flush") == 1
    assert len(session.calls) == EXPECTED_CALLS
    # And it did real work, so the count is not trivially low.
    assert decision.evidence_snapshot["decision"]["decision"]


async def test_a_repeated_decision_costs_exactly_one_read() -> None:
    """Contract §5.3 idempotency is not just "same answer": re-deciding would
    re-scan the wardrobe, and a wardrobe that changed in between would produce
    a different answer — which is what the snapshot exists to prevent."""
    existing = WardrobeDecision(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        decision_type="purchase",
        context_snapshot={},
        evidence_snapshot={},
        decision="worth_adding",
        uncertainty="low",
        visual_need="none",
    )
    session = DecisionSession(_wardrobe(20), existing=existing)

    returned = await DecidePurchase(session).execute(uuid.uuid4(), _candidate())  # type: ignore[arg-type]

    assert returned is existing
    assert session.calls == ["scalar"]
    assert not session.added


async def test_an_unparsed_candidate_still_costs_only_the_fixed_reads() -> None:
    """`parse_failed` degrades to `insufficient_information` without a wardrobe
    scan: neither evidence module queries when it has no category to look for."""
    session = DecisionSession(_wardrobe(20))

    decision = await DecidePurchase(session).execute(  # type: ignore[arg-type]
        uuid.uuid4(), _candidate(parsed={})
    )

    assert session.calls == ["scalar", "scalar", "flush"]
    assert decision.decision == "insufficient_information"


async def test_the_decision_is_written_once() -> None:
    session = DecisionSession(_wardrobe(6))

    await DecidePurchase(session).execute(uuid.uuid4(), _candidate())  # type: ignore[arg-type]

    assert len(session.added) == 1
    assert isinstance(session.added[0], WardrobeDecision)
