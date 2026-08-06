"""V3-003 — persistence guarantees for purchase candidates and decisions.

The three things these tests defend:

1. what we persist as durable truth is a stable reference, never a signed URL;
2. a decision snapshot never changes after the wardrobe it described changes;
3. the frozen contract enums are enforced by the database, not by convention.
"""

import copy
import uuid
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import (
    Category,
    DecisionUncertainty,
    DecisionVisualNeed,
    ItemStatus,
    PurchaseCandidateSource,
    PurchaseCandidateStatus,
    PurchaseDecision,
    WardrobeDecisionType,
)
from app.infrastructure.persistence.models.purchase_decision import (
    DECISION_SNAPSHOT_VERSION,
    PurchaseCandidate,
    WardrobeDecision,
)
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.purchase_decision_repo import (
    SqlAlchemyPurchaseCandidateRepository,
    SqlAlchemyWardrobeDecisionRepository,
)


async def _user(session: AsyncSession) -> User:
    user = User()
    session.add(user)
    await session.flush()
    return user


def _candidate(user_id: uuid.UUID, **overrides: Any) -> PurchaseCandidate:
    kwargs: dict[str, Any] = {
        "user_id": user_id,
        "source_type": PurchaseCandidateSource.SCREENSHOT.value,
        "source_object_key": "users/abc/shopping/candidate.jpg",
        "status": PurchaseCandidateStatus.PARSED.value,
        "parsed_item": {
            "category": "outerwear",
            "colors": ["camel"],
            "silhouette": "oversized",
            "material_hints": ["wool"],
            "price": "CNY 1299",
            "scene_tags": ["commute"],
        },
    }
    kwargs.update(overrides)
    return PurchaseCandidate(**kwargs)


def _decision(user_id: uuid.UUID, candidate_id: uuid.UUID | None, **overrides: Any) -> WardrobeDecision:
    kwargs: dict[str, Any] = {
        "user_id": user_id,
        "decision_type": WardrobeDecisionType.PURCHASE.value,
        "candidate_id": candidate_id,
        "snapshot_version": DECISION_SNAPSHOT_VERSION,
        "context_snapshot": {"concern": "redundancy"},
        "evidence_snapshot": {"reasons": []},
        "decision": PurchaseDecision.WORTH_ADDING.value,
        "uncertainty": DecisionUncertainty.LOW.value,
        "visual_need": DecisionVisualNeed.NONE.value,
    }
    kwargs.update(overrides)
    return WardrobeDecision(**kwargs)


async def test_candidate_persists_a_stable_reference_not_a_signed_url(
    db_session: AsyncSession,
) -> None:
    user = await _user(db_session)
    repo = SqlAlchemyPurchaseCandidateRepository(db_session)

    stored = await repo.add(_candidate(user.id))

    fetched = await repo.get_for_user(stored.id, user.id)
    assert fetched is not None
    assert fetched.source_object_key == "users/abc/shopping/candidate.jpg"
    assert fetched.source_url is None
    assert fetched.parsed_item is not None
    assert fetched.parsed_item["category"] == "outerwear"

    # Whitelist, not a smell test: adding an `image_url`-style column later
    # would reintroduce a TTL-bound value as durable truth, so it has to be a
    # deliberate edit here (V3-003 acceptance 1).
    assert {column.name for column in PurchaseCandidate.__table__.columns} == {
        "id",
        "user_id",
        "source_type",
        "source_url",
        "source_object_key",
        "status",
        "parsed_item",
        "created_at",
        "updated_at",
    }


async def test_candidate_scoped_to_its_owner(db_session: AsyncSession) -> None:
    owner = await _user(db_session)
    other = await _user(db_session)
    repo = SqlAlchemyPurchaseCandidateRepository(db_session)
    stored = await repo.add(_candidate(owner.id))

    assert await repo.get_for_user(stored.id, other.id) is None
    assert await repo.list_for_user(other.id) == []


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param(
            {"source_url": "https://shop.example/item/1"},
            id="both_references",
        ),
        pytest.param(
            {"source_object_key": None},
            id="no_reference",
        ),
    ],
)
async def test_candidate_requires_exactly_one_source_reference(
    db_session: AsyncSession, overrides: dict[str, Any]
) -> None:
    user = await _user(db_session)
    db_session.add(_candidate(user.id, **overrides))

    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"source_type": "carrier_pigeon"}, id="source_type"),
        pytest.param({"status": "almost_parsed"}, id="status"),
    ],
)
async def test_candidate_enum_columns_are_enforced_by_the_database(
    db_session: AsyncSession, overrides: dict[str, Any]
) -> None:
    user = await _user(db_session)
    db_session.add(_candidate(user.id, **overrides))

    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


@pytest.mark.parametrize(
    "overrides",
    [
        pytest.param({"decision": "probably_fine"}, id="decision"),
        pytest.param({"uncertainty": "unsure"}, id="uncertainty"),
        pytest.param({"visual_need": "mandatory"}, id="visual_need"),
    ],
)
async def test_decision_enum_columns_are_enforced_by_the_database(
    db_session: AsyncSession, overrides: dict[str, Any]
) -> None:
    user = await _user(db_session)
    candidate = await SqlAlchemyPurchaseCandidateRepository(db_session).add(_candidate(user.id))
    db_session.add(_decision(user.id, candidate.id, **overrides))

    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_snapshot_survives_the_wardrobe_it_described(db_session: AsyncSession) -> None:
    """The reason this table exists: renaming and then deleting the compared
    item must leave the historical explanation byte-identical."""
    user = await _user(db_session)
    item = WardrobeItem(
        user_id=user.id,
        title="Camel wool coat",
        category=Category.OUTERWEAR.value,
        color_main="camel",
        status=ItemStatus.WEARABLE.value,
    )
    db_session.add(item)
    await db_session.flush()

    candidate = await SqlAlchemyPurchaseCandidateRepository(db_session).add(_candidate(user.id))
    decisions = SqlAlchemyWardrobeDecisionRepository(db_session)
    context = {
        "concern": "redundancy",
        "wardrobe_item_count": 1,
        "candidate": {"category": "outerwear", "colors": ["camel"]},
    }
    evidence = {
        "decision_text": "You already own this coat in the same colour.",
        "reasons": [{"kind": "redundancy", "text": "Overlaps your camel wool coat."}],
        "closet_comparison": {
            "summary_text": "1 similar item",
            "similar_items": [{"item_id": str(item.id), "relation": "covers"}],
        },
        "next_actions": ["skip", "style_with_existing"],
        "boundary_note": "This is not a size or fit guarantee.",
    }
    stored = await decisions.add(
        _decision(
            user.id,
            candidate.id,
            context_snapshot=copy.deepcopy(context),
            evidence_snapshot=copy.deepcopy(evidence),
            decision=PurchaseDecision.AVOID_DUPLICATE.value,
            uncertainty=DecisionUncertainty.MEDIUM.value,
        )
    )
    decision_id = stored.id
    # Keep the ids as plain values: `expire_all()` below would otherwise make
    # reading `user.id` a lazy refresh, which is illegal IO under asyncio.
    user_id, item_id = user.id, item.id

    item.title = "Renamed coat"
    await db_session.flush()
    await db_session.delete(item)
    await db_session.flush()
    # Drop everything from the identity map so the assertions below read the
    # snapshot back out of Postgres rather than out of memory.
    db_session.expire_all()

    replayed = await decisions.get_for_user(decision_id, user_id)
    assert replayed is not None
    assert replayed.context_snapshot == context
    assert replayed.evidence_snapshot == evidence
    assert replayed.decision == PurchaseDecision.AVOID_DUPLICATE.value
    assert replayed.snapshot_version == DECISION_SNAPSHOT_VERSION
    # The deleted item is still named inside the snapshot; nothing cascaded.
    assert await db_session.scalar(select(WardrobeItem).where(WardrobeItem.id == item_id)) is None


async def test_decision_table_and_repository_are_append_only() -> None:
    assert "updated_at" not in WardrobeDecision.__table__.columns
    public_methods = {name for name in vars(SqlAlchemyWardrobeDecisionRepository) if not name.startswith("_")}
    assert public_methods == {"add", "get_for_user", "get_for_candidate"}


async def test_one_decision_per_candidate(db_session: AsyncSession) -> None:
    user = await _user(db_session)
    candidate = await SqlAlchemyPurchaseCandidateRepository(db_session).add(_candidate(user.id))
    decisions = SqlAlchemyWardrobeDecisionRepository(db_session)
    await decisions.add(_decision(user.id, candidate.id))

    assert await decisions.get_for_candidate(candidate.id, user.id) is not None

    db_session.add(_decision(user.id, candidate.id))
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_decisions_without_a_candidate_are_not_deduplicated(
    db_session: AsyncSession,
) -> None:
    """The unique index is on candidate_id; NULLs must stay unconstrained so a
    future outfit-scoped decision type is not limited to one row per user."""
    user = await _user(db_session)
    decisions = SqlAlchemyWardrobeDecisionRepository(db_session)
    await decisions.add(_decision(user.id, None, outfit_id=uuid.uuid4()))
    await decisions.add(_decision(user.id, None, outfit_id=uuid.uuid4()))

    rows = await db_session.scalars(select(WardrobeDecision).where(WardrobeDecision.user_id == user.id))
    assert len(list(rows)) == 2


async def test_account_deletion_cascades_candidates_and_decisions(
    db_session: AsyncSession,
) -> None:
    user = await _user(db_session)
    candidate = await SqlAlchemyPurchaseCandidateRepository(db_session).add(_candidate(user.id))
    await SqlAlchemyWardrobeDecisionRepository(db_session).add(_decision(user.id, candidate.id))

    await db_session.delete(user)
    await db_session.flush()

    assert (await db_session.scalars(select(PurchaseCandidate))).all() == []
    assert (await db_session.scalars(select(WardrobeDecision))).all() == []
