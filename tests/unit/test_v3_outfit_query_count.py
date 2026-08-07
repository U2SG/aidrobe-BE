"""V3-006 — the query budget, counted exactly, without a database.

Same guard as V3-005's equivalent and for the same reason: the obvious
implementation (load items, then look up each item's wear scenes) is an N+1
that turns a 300-item wardrobe into 301 round-trips. The session double counts
the calls and runs anywhere, so this stays local evidence as well as CI
evidence; `tests/services/test_v3_outfit_evidence.py` re-checks the invariant
against real emitted SQL.

Runs under `pytest --noconftest`.
"""

from __future__ import annotations

import uuid

import pytest

from app.application.purchase.candidate_normalizer import ParsedItem
from app.application.purchase.compose_outfit_evidence import compose_outfit_evidence
from app.application.purchase.outfit_composition import (
    OUTFIT_STATUS_COMPOSED,
    OUTFIT_STATUS_UNKNOWN_CANDIDATE,
)
from app.application.purchase.wardrobe_similarity import (
    RELATION_COMPLEMENTS,
    RELATION_COVERS,
    ClosetEvidence,
    SimilarItemEvidence,
)
from app.common.enums import Category, ItemStatus
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from tests.unit.test_v3_similarity_query_count import RecordingSession

# The budget this feature commits to, independent of wardrobe size. There is no
# vector tier here: V3-006 ranks *within* a slot over items already loaded.
EXPECTED_QUERIES = 2  # the availability scan, then the batched wear scenes


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
    return ParsedItem(category=Category.TOP.value, colors=["gray"], scene_tags=["commute"])


def _wardrobe(size: int) -> list[WardrobeItem]:
    """A pool that can actually complete an outfit, padded to `size`."""
    items = [_item(), _item(category=Category.SHOES.value)]
    items.extend(_item() for _ in range(max(0, size - len(items))))
    return items


@pytest.mark.parametrize("wardrobe_size", [2, 10, 300])
async def test_query_count_is_constant_in_wardrobe_size(wardrobe_size: int) -> None:
    session = RecordingSession(_wardrobe(wardrobe_size))

    evidence = await compose_outfit_evidence(session, uuid.uuid4(), _candidate())

    assert session.query_count == EXPECTED_QUERIES
    assert session.calls == ["scalars", "execute"]
    # And it did real work, so the count is not trivially low.
    assert evidence.status == OUTFIT_STATUS_COMPOSED


async def test_wear_scenes_are_fetched_in_one_batch() -> None:
    items = _wardrobe(50)
    session = RecordingSession(items, wear_scene_rows=[(i.id, "commute") for i in items])

    await compose_outfit_evidence(session, uuid.uuid4(), _candidate())

    assert session.calls.count("execute") == 1


async def test_an_empty_wardrobe_skips_the_scene_query() -> None:
    session = RecordingSession([])

    await compose_outfit_evidence(session, uuid.uuid4(), _candidate())

    assert session.calls == ["scalars"]


@pytest.mark.parametrize("candidate", [ParsedItem(), ParsedItem(category="accessory")])
async def test_an_unplaceable_candidate_costs_no_queries_at_all(candidate: ParsedItem) -> None:
    """A failed V3-004 parse must not touch the database to say "unknown" — and
    neither must a category that occupies no outfit slot, since there is nothing
    to compose around and therefore nothing worth loading."""
    session = RecordingSession([_item()])

    evidence = await compose_outfit_evidence(session, uuid.uuid4(), candidate)

    assert session.query_count == 0
    assert evidence.status == OUTFIT_STATUS_UNKNOWN_CANDIDATE


async def test_alternatives_cost_no_extra_query() -> None:
    """The similar items ride the same scan as a pinned window, so acceptance 2
    is free — a per-alternative lookup would be the N+1 in a second costume."""
    items = _wardrobe(4)
    session = RecordingSession(items)
    closet = ClosetEvidence(
        redundancy_level="high",
        similar_items=(
            SimilarItemEvidence(item_id=items[0].id, relation=RELATION_COVERS, match_reasons=()),
            SimilarItemEvidence(
                item_id=items[1].id, relation=RELATION_COMPLEMENTS, match_reasons=()
            ),
        ),
    )

    await compose_outfit_evidence(session, uuid.uuid4(), _candidate(), closet)

    assert session.query_count == EXPECTED_QUERIES
