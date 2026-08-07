"""V3-005 — similarity evidence against a real Postgres.

What only a database can prove, and what is therefore here rather than in the
two `tests/unit` modules: that the filters actually filter (other users' items,
soft-deleted and non-wearable items), that `fit_feedback.scene` really joins to
the right item, that the pgvector branch degrades instead of erroring, and that
the query budget holds when the counting is over emitted SQL.

On the pgvector branch: `PGVECTOR_ENABLED` is unset in CI, so
`models.embedding._USE_VECTOR` is false and `ItemEmbedding.vector` is a JSONB
column. Flipping the env var at runtime therefore selects the vector *branch*
without giving it a vector *index* — which is the honest limit of what this
environment can test, and is why the flag-on cases here assert degradation to
the deterministic tier rather than a cosine ordering. A cosine-ordered result
is covered as pure output handling in `tests/unit/test_v3_wardrobe_similarity`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.purchase import compute_similarity_evidence as evidence_module
from app.application.purchase.candidate_normalizer import ParsedItem
from app.application.purchase.compute_similarity_evidence import compute_similarity_evidence
from app.application.purchase.wardrobe_similarity import (
    REDUNDANCY_HIGH,
    REDUNDANCY_NONE,
    REDUNDANCY_UNKNOWN,
    RELATION_COMPLEMENTS,
    RELATION_COVERS,
    SCENE_SOURCE_CATEGORY_DEFAULT,
    SCENE_SOURCE_WEAR_FEEDBACK,
)
from app.common.enums import Category, FitFeedbackType, FitScene, ItemStatus, WardrobeItemStatus
from app.infrastructure.persistence.models.fit import FitFeedback
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
    title: str = "coat",
    category: str = Category.OUTERWEAR.value,
    color: str = "navy",
    status: str = ItemStatus.WEARABLE.value,
    material: str | None = None,
    style_tags: object = None,
    deleted: bool = False,
    created_at: datetime | None = None,
) -> WardrobeItem:
    # `created_at` is explicit wherever the scan's `ORDER BY created_at DESC`
    # decides the outcome: the column is `server_default=func.now()` and
    # Postgres' `now()` is transaction-scoped, so every row seeded by one test
    # otherwise shares a single timestamp and the ordering is arbitrary.
    item = WardrobeItem(
        user_id=user_id,
        title=title,
        category=category,
        color_main=color,
        status=status,
        material=material,
        style_tags=style_tags,
        deleted_at=datetime.now(UTC) if deleted else None,
        **({"created_at": created_at} if created_at is not None else {}),
    )
    session.add(item)
    await session.flush()
    return item


async def _rate_scene(
    session: AsyncSession,
    user_id: uuid.UUID,
    item: WardrobeItem,
    scene: str,
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


def _candidate(**overrides: object) -> ParsedItem:
    base: dict[str, object] = {
        "category": Category.OUTERWEAR.value,
        "colors": ["gray"],
        "material_hints": ["wool"],
        "scene_tags": [FitScene.COMMUTE.value],
    }
    base.update(overrides)
    return ParsedItem(**base)


@contextmanager
def _count_sql() -> Iterator[list[str]]:
    """Every statement the block emits, in order."""
    statements: list[str] = []

    def _before(conn, cursor, statement, parameters, context, executemany) -> None:
        statements.append(statement)

    event.listen(Engine, "before_cursor_execute", _before)
    try:
        yield statements
    finally:
        event.remove(Engine, "before_cursor_execute", _before)


# --- Filters ------------------------------------------------------------


async def test_only_the_callers_own_wearable_items_are_compared(
    db_session: AsyncSession,
) -> None:
    user_id = await _seed_user(db_session)
    stranger_id = await _seed_user(db_session)
    mine = await _add_item(db_session, user_id, title="mine", color="gray")
    await _add_item(db_session, stranger_id, title="theirs", color="gray")
    await _add_item(db_session, user_id, title="binned", color="gray", deleted=True)
    await _add_item(
        db_session, user_id, title="unavailable", color="gray", status=ItemStatus.UNAVAILABLE.value
    )

    evidence = await compute_similarity_evidence(db_session, user_id, _candidate())

    assert [s.item_id for s in evidence.similar_items] == [mine.id]
    assert evidence.redundancy_level == REDUNDANCY_HIGH


async def test_complementary_categories_are_loaded_in_the_same_pass(
    db_session: AsyncSession,
) -> None:
    user_id = await _seed_user(db_session)
    coat = await _add_item(db_session, user_id, title="coat", color="gray")
    trousers = await _add_item(db_session, user_id, title="trousers", category=Category.BOTTOM.value)
    await _add_item(db_session, user_id, title="dress", category=Category.DRESS.value)

    evidence = await compute_similarity_evidence(db_session, user_id, _candidate())

    relations = {s.item_id: s.relation for s in evidence.similar_items}
    assert relations[coat.id] == RELATION_COVERS
    assert relations[trousers.id] == RELATION_COMPLEMENTS
    assert evidence.compatible_item_ids == [trousers.id]


async def test_empty_wardrobe_reports_none_not_unknown(db_session: AsyncSession) -> None:
    user_id = await _seed_user(db_session)
    evidence = await compute_similarity_evidence(db_session, user_id, _candidate())
    assert evidence.redundancy_level == REDUNDANCY_NONE
    assert evidence.similar_items == ()


async def test_unparsed_candidate_reports_unknown(db_session: AsyncSession) -> None:
    user_id = await _seed_user(db_session)
    await _add_item(db_session, user_id, color="gray")

    for degraded in (None, ParsedItem(), ParsedItem(colors=["gray"])):
        evidence = await compute_similarity_evidence(db_session, user_id, degraded)
        assert evidence.redundancy_level == REDUNDANCY_UNKNOWN


# --- Scene overlap, joined from real fit feedback -----------------------


async def test_wear_feedback_grounds_scene_coverage(db_session: AsyncSession) -> None:
    user_id = await _seed_user(db_session)
    worn_for_commute = await _add_item(db_session, user_id, title="worn", color="gray")
    never_rated = await _add_item(db_session, user_id, title="unrated", color="gray")
    await _rate_scene(db_session, user_id, worn_for_commute, FitScene.COMMUTE.value)

    evidence = await compute_similarity_evidence(db_session, user_id, _candidate())

    assert len(evidence.coverage_scenarios) == 1
    coverage = evidence.coverage_scenarios[0]
    assert coverage.scene == FitScene.COMMUTE.value
    assert coverage.source == SCENE_SOURCE_WEAR_FEEDBACK
    assert coverage.covered_by_item_ids == (worn_for_commute.id,)
    assert never_rated.id not in coverage.covered_by_item_ids


async def test_feedback_on_another_users_item_cannot_ground_coverage(
    db_session: AsyncSession,
) -> None:
    """The scene batch keys on item id; the item query is what scopes the user.
    If those two ever disagree, a stranger's wear history would leak in here."""
    user_id = await _seed_user(db_session)
    stranger_id = await _seed_user(db_session)
    stranger_item = await _add_item(db_session, stranger_id, title="theirs", color="gray")
    await _rate_scene(db_session, stranger_id, stranger_item, FitScene.COMMUTE.value)
    await _add_item(db_session, user_id, title="mine", color="gray")

    evidence = await compute_similarity_evidence(db_session, user_id, _candidate())

    assert evidence.coverage_scenarios[0].source == SCENE_SOURCE_CATEGORY_DEFAULT
    assert stranger_item.id not in evidence.coverage_scenarios[0].covered_by_item_ids


async def test_style_tags_are_read_from_the_persisted_jsonb_shape(
    db_session: AsyncSession,
) -> None:
    user_id = await _seed_user(db_session)
    tagged = await _add_item(
        db_session, user_id, title="tagged", color="navy", style_tags={"tags": ["commute"]}
    )

    evidence = await compute_similarity_evidence(db_session, user_id, _candidate())

    assert evidence.similar_items[0].item_id == tagged.id
    assert "shared_scene" in evidence.similar_items[0].match_reasons


# --- A1/A4: the ranking cascade -----------------------------------------


async def test_structured_ranking_orders_by_shared_attributes(
    db_session: AsyncSession,
) -> None:
    """The deterministic tier, which is the path CI exercises."""
    user_id = await _seed_user(db_session)
    bare = await _add_item(db_session, user_id, title="bare", color="navy")
    material_match = await _add_item(db_session, user_id, title="wool", color="navy", material="wool")
    color_match = await _add_item(db_session, user_id, title="gray", color="gray")

    evidence = await compute_similarity_evidence(db_session, user_id, _candidate())

    assert [s.item_id for s in evidence.similar_items] == [
        color_match.id,
        material_match.id,
        bare.id,
    ]


async def test_pgvector_enabled_without_embeddings_degrades_to_the_same_answer(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A4 — flag on, nothing embedded: the vector tier finds no anchor and the
    deterministic tier produces the identical evidence. No error, no empty
    result, no dependence on the extension being installed."""
    user_id = await _seed_user(db_session)
    await _add_item(db_session, user_id, title="bare", color="navy")
    await _add_item(db_session, user_id, title="gray", color="gray")

    monkeypatch.delenv("PGVECTOR_ENABLED", raising=False)
    off = await compute_similarity_evidence(db_session, user_id, _candidate())

    monkeypatch.setenv("PGVECTOR_ENABLED", "true")
    on = await compute_similarity_evidence(db_session, user_id, _candidate())

    assert on.as_payload() == off.as_payload()
    assert on.redundancy_level == REDUNDANCY_HIGH


# --- Query budget, counted over emitted SQL -----------------------------


async def test_sql_count_does_not_grow_with_the_wardrobe(
    db_session: AsyncSession,
) -> None:
    """The N+1 guard against a real database.

    Asserted as "constant, and small" rather than as one magic number: the
    exact figure this counter sees also depends on connection-level statements
    the driver may emit, while the property V3-005 actually commits to is that
    a bigger wardrobe does not cost more round-trips. The exact per-call budget
    is pinned in `tests/unit/test_v3_similarity_query_count.py`.
    """
    user_id = await _seed_user(db_session)
    for _ in range(3):
        await _add_item(db_session, user_id, color="gray")

    with _count_sql() as small:
        await compute_similarity_evidence(db_session, user_id, _candidate())

    for _ in range(40):
        await _add_item(db_session, user_id, color="gray")

    with _count_sql() as large:
        evidence = await compute_similarity_evidence(db_session, user_id, _candidate())

    assert len(small) == len(large), f"query count grew: {len(small)} -> {len(large)}"
    assert len(large) <= 4, f"expected a bounded budget, emitted {len(large)}: {large}"
    # 43 owned coats: the displayed list stays capped, the count does not. The
    # count is what tells the user they own 43 of these; `len(similar_items)`
    # would tell them 5.
    assert evidence.similar_item_count == 43
    assert len(evidence.similar_items) == 5
    assert evidence.same_category_scan_truncated is False


# --- P1 regression: complements must not displace same-category items ----


async def test_a_complement_heavy_wardrobe_cannot_hide_the_matching_coat(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The displacement bug, against a real database.

    One `LIMIT` across the candidate's category *and* its complements meant the
    most recent items could fill the whole window with trousers and shoes,
    loading zero coats — and the evidence then read `redundancy_level="none"`,
    a confident "you own nothing like this" for someone who owns a matching
    coat. Wrong in the direction that encourages the purchase.

    The window is shrunk rather than seeding 200+ rows: the bug is about the
    window being *shared*, not about its size. Timestamps are explicit and
    descending, with the coat oldest, so the pre-fix code would deterministically
    drop it.
    """
    monkeypatch.setattr(evidence_module, "MAX_ITEMS_PER_CATEGORY_GROUP", 3)
    user_id = await _seed_user(db_session)
    base = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)

    for offset, category in enumerate(
        [Category.BOTTOM.value, Category.SHOES.value, Category.TOP.value, Category.BOTTOM.value]
    ):
        await _add_item(
            db_session,
            user_id,
            title=f"complement-{offset}",
            category=category,
            created_at=base - timedelta(minutes=offset),
        )
    coat = await _add_item(
        db_session,
        user_id,
        title="the oldest coat",
        color="gray",
        created_at=base - timedelta(days=30),
    )

    evidence = await compute_similarity_evidence(db_session, user_id, _candidate())

    assert evidence.redundancy_level == REDUNDANCY_HIGH
    assert coat.id in [s.item_id for s in evidence.similar_items]
    assert evidence.similar_item_count == 1
    # The same-category window held one coat out of a window of three, so the
    # count is a true total and must not be flagged as a floor.
    assert evidence.same_category_scan_truncated is False


async def test_a_saturated_same_category_window_declares_itself(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """More coats than the window holds: the count becomes an explicit floor,
    rather than a total that silently understates the wardrobe."""
    monkeypatch.setattr(evidence_module, "MAX_ITEMS_PER_CATEGORY_GROUP", 3)
    user_id = await _seed_user(db_session)
    base = datetime(2026, 8, 7, 12, 0, tzinfo=UTC)
    for offset in range(5):
        await _add_item(
            db_session,
            user_id,
            title=f"coat-{offset}",
            color="gray",
            created_at=base - timedelta(minutes=offset),
        )

    evidence = await compute_similarity_evidence(db_session, user_id, _candidate())

    assert evidence.similar_item_count == 3
    assert evidence.same_category_scan_truncated is True


async def test_an_item_in_the_laundry_basket_still_counts_as_owned(
    db_session: AsyncSession,
) -> None:
    """`dirty` is a temporary state with a documented route back to `wearable`
    (`docs/wardrobe-status-machine.md`), so it is still a coat the user owns.
    Excluding it would understate redundancy; `donated` genuinely is gone."""
    user_id = await _seed_user(db_session)
    in_the_wash = await _add_item(
        db_session, user_id, title="dirty", color="gray", status=WardrobeItemStatus.DIRTY.value
    )
    await _add_item(
        db_session, user_id, title="given away", color="gray", status=WardrobeItemStatus.DONATED.value
    )

    evidence = await compute_similarity_evidence(db_session, user_id, _candidate())

    assert [s.item_id for s in evidence.similar_items] == [in_the_wash.id]
    assert evidence.redundancy_level == REDUNDANCY_HIGH
