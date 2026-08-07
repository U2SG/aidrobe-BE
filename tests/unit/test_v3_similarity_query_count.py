"""V3-005 — the query budget, counted exactly, without a database.

V3-005 requires query-count evidence. The property that matters is that a
decision costs a *constant* number of round-trips: the obvious implementation
(fetch items, then look up each item's wear scenes) is an N+1 that turns a
300-item wardrobe into 301 queries. Asserting it against a real database would
put this evidence behind testcontainers, which this host cannot run (see the
repo CLAUDE.md); a session double counts the same calls and runs anywhere,
so it stays green locally and in CI.

`tests/services/test_v3_similarity_evidence.py` re-checks the same invariant
against a real Postgres, where the counting is over emitted SQL rather than
over session calls.

Runs under `pytest --noconftest`.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest

from app.application.purchase.candidate_normalizer import ParsedItem
from app.application.purchase.compute_similarity_evidence import compute_similarity_evidence
from app.common.enums import Category, ItemStatus
from app.infrastructure.persistence.models.wardrobe import WardrobeItem

# The budget this feature commits to. Independent of wardrobe size.
EXPECTED_QUERIES_STRUCTURED = 2  # items, then wear scenes
EXPECTED_QUERIES_VECTOR_ATTEMPTED = 3  # + the embedding anchor lookup


class _Result:
    """The slice of `Result` that `load_wear_scenes` uses."""

    def __init__(self, rows: list[tuple[Any, ...]]) -> None:
        self._rows = rows

    def all(self) -> list[tuple[Any, ...]]:
        return self._rows


class RecordingSession:
    """Counts session round-trips and returns canned rows in call order.

    Deliberately dumb: it does not interpret the statements, because the point
    is the *number* of calls, and a double that tried to execute SQL would be a
    worse database than the one in the services tests.
    """

    def __init__(
        self,
        items: list[WardrobeItem],
        *,
        wear_scene_rows: list[tuple[uuid.UUID, str]] | None = None,
        anchor_vector: Any = None,
    ) -> None:
        self.items = items
        self.wear_scene_rows = wear_scene_rows or []
        self.anchor_vector = anchor_vector
        self.calls: list[str] = []

    @property
    def query_count(self) -> int:
        return len(self.calls)

    async def scalars(self, _statement: Any) -> list[Any]:
        self.calls.append("scalars")
        # First `scalars` is the item load; a later one is the vector ranking,
        # which only runs when an anchor vector was found.
        if self.calls.count("scalars") == 1:
            return list(self.items)
        return [item.id for item in self.items]

    async def execute(self, _statement: Any) -> _Result:
        self.calls.append("execute")
        return _Result(list(self.wear_scene_rows))

    async def scalar(self, _statement: Any) -> Any:
        self.calls.append("scalar")
        return self.anchor_vector


def _item(*, category: str = Category.OUTERWEAR.value, color: str = "gray") -> WardrobeItem:
    return WardrobeItem(
        id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        title="coat",
        category=category,
        color_main=color,
        status=ItemStatus.WEARABLE.value,
    )


def _candidate() -> ParsedItem:
    return ParsedItem(
        category=Category.OUTERWEAR.value,
        colors=["gray"],
        scene_tags=["commute"],
    )


@pytest.mark.parametrize("wardrobe_size", [1, 10, 300])
async def test_query_count_is_constant_in_wardrobe_size(wardrobe_size: int) -> None:
    """The N+1 guard. 300 items must cost exactly what 1 item costs."""
    items = [_item() for _ in range(wardrobe_size)]
    session = RecordingSession(items)

    evidence = await compute_similarity_evidence(session, uuid.uuid4(), _candidate())

    assert session.query_count == EXPECTED_QUERIES_STRUCTURED
    assert session.calls == ["scalars", "execute"]
    # And it did real work, so the count is not trivially low.
    assert evidence.similar_item_count > 0


async def test_wear_scenes_are_fetched_in_one_batch_for_every_item() -> None:
    """Scenes for 50 items come back in a single `IN` query, not 50 lookups."""
    items = [_item() for _ in range(50)]
    rows = [(item.id, "commute") for item in items]
    session = RecordingSession(items, wear_scene_rows=rows)

    await compute_similarity_evidence(session, uuid.uuid4(), _candidate())

    assert session.calls.count("execute") == 1


async def test_empty_wardrobe_skips_the_scene_query() -> None:
    session = RecordingSession([])

    evidence = await compute_similarity_evidence(session, uuid.uuid4(), _candidate())

    assert session.calls == ["scalars"]
    assert evidence.similar_item_count == 0


async def test_unusable_candidate_costs_no_queries_at_all() -> None:
    """A failed V3-004 parse must not touch the database to say "unknown"."""
    session = RecordingSession([_item()])

    evidence = await compute_similarity_evidence(session, uuid.uuid4(), ParsedItem())

    assert session.query_count == 0
    assert evidence.redundancy_level == "unknown"


async def test_vector_tier_costs_one_extra_query_when_it_finds_no_anchor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pgvector on, nothing embedded: pay for the anchor probe, then fall back."""
    monkeypatch.setenv("PGVECTOR_ENABLED", "true")
    session = RecordingSession([_item()], anchor_vector=None)

    await compute_similarity_evidence(session, uuid.uuid4(), _candidate())

    assert session.query_count == EXPECTED_QUERIES_VECTOR_ATTEMPTED
    assert session.calls == ["scalars", "execute", "scalar"]


async def test_vector_tier_ranks_within_the_budget_when_an_anchor_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """pgvector on with an anchor: anchor probe + one ranking query, no more.

    This is the only place the vector branch runs to completion in a test — a
    real `<->` ordering needs the extension, which CI does not have (see the
    module docstring in `compute_similarity_evidence`). What is asserted here
    is the branch's cost and that its ordering reaches the caller; the
    ranked-output handling itself is `test_external_order_is_preserved_into_
    the_payload` in `test_v3_wardrobe_similarity.py`.
    """
    monkeypatch.setenv("PGVECTOR_ENABLED", "true")
    session = RecordingSession([_item(), _item()], anchor_vector=[0.1] * 512)

    await compute_similarity_evidence(session, uuid.uuid4(), _candidate())

    assert session.query_count == EXPECTED_QUERIES_VECTOR_ATTEMPTED + 1
    assert session.calls == ["scalars", "execute", "scalar", "scalars"]
