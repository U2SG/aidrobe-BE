import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import Category, ItemStatus
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.models.wardrobe_relationship import (
    WardrobeItemRelationship,
)
from app.infrastructure.persistence.repositories.wardrobe_relationship_repo import (
    SqlAlchemyWardrobeRelationshipRepository,
)


async def _item(
    session: AsyncSession,
    user_id: uuid.UUID,
    title: str,
    category: str,
    color: str,
) -> WardrobeItem:
    item = WardrobeItem(
        user_id=user_id,
        title=title,
        category=category,
        color_main=color,
        status=ItemStatus.WEARABLE.value,
    )
    session.add(item)
    await session.flush()
    return item


async def test_relationship_creation_and_retrieval(db_session: AsyncSession) -> None:
    user = User()
    db_session.add(user)
    await db_session.flush()
    shirt = await _item(db_session, user.id, "White shirt", Category.TOP.value, "white")
    pants = await _item(db_session, user.id, "Black pants", Category.BOTTOM.value, "black")

    repo = SqlAlchemyWardrobeRelationshipRepository(db_session)
    created = await repo.upsert_relationship(
        source_item_id=shirt.id,
        target_item_id=pants.id,
        relationship_type="pairs_well",
        score=0.8,
        confidence=0.9,
        source="user_feedback",
        metadata_json={"reason": "office staple"},
    )

    rows = await repo.positive_pairs(shirt.id)
    assert [row.id for row in rows] == [created.id]
    assert {rows[0].source_item_id, rows[0].target_item_id} == {shirt.id, pants.id}
    assert rows[0].metadata_json == {"reason": "office staple"}


async def test_rejected_pairs_filtering(db_session: AsyncSession) -> None:
    user = User()
    db_session.add(user)
    await db_session.flush()
    shirt = await _item(db_session, user.id, "White shirt", Category.TOP.value, "white")
    pants = await _item(db_session, user.id, "Black pants", Category.BOTTOM.value, "black")
    shoes = await _item(db_session, user.id, "Green shoes", Category.SHOES.value, "green")

    repo = SqlAlchemyWardrobeRelationshipRepository(db_session)
    rejected = await repo.upsert_relationship(
        source_item_id=shirt.id,
        target_item_id=pants.id,
        relationship_type="user_rejected_pairing",
        score=-0.9,
        confidence=1.0,
        source="user_feedback",
    )
    negative_score = await repo.upsert_relationship(
        source_item_id=shirt.id,
        target_item_id=shoes.id,
        relationship_type="style_clash",
        score=-0.4,
        confidence=0.7,
        source="rule_based",
    )
    await repo.upsert_relationship(
        source_item_id=pants.id,
        target_item_id=shoes.id,
        relationship_type="pairs_well",
        score=0.7,
        confidence=0.8,
        source="rule_based",
    )

    rows = await repo.rejected_pairs(shirt.id)
    assert {row.id for row in rows} == {rejected.id, negative_score.id}


async def test_regenerate_replaces_stale_rule_based_edge(db_session: AsyncSession) -> None:
    """When a pair's derived type changes (color edit flips a style_clash into a
    pairs_well), regenerating must leave ONLY the new rule_based edge — no stale
    row — and must not touch user_feedback edges."""
    user = User()
    db_session.add(user)
    await db_session.flush()
    # red + green is a COLOR_CONFLICT → first generation derives style_clash.
    top = await _item(db_session, user.id, "Red top", Category.TOP.value, "red")
    bottom = await _item(db_session, user.id, "Green pants", Category.BOTTOM.value, "green")

    repo = SqlAlchemyWardrobeRelationshipRepository(db_session)
    # A user-confirmed edge on the same pair: must survive the rule_based wipe.
    feedback = await repo.upsert_relationship(
        source_item_id=top.id,
        target_item_id=bottom.id,
        relationship_type="user_approved_pairing",
        score=0.9,
        confidence=1.0,
        source="user_feedback",
    )

    await repo.delete_rule_based_for_user(user.id)
    await repo.generate_rule_based([top, bottom])
    await db_session.flush()
    rule_rows = list(
        await db_session.scalars(
            select(WardrobeItemRelationship).where(
                WardrobeItemRelationship.source == "rule_based"
            )
        )
    )
    assert [row.relationship_type for row in rule_rows] == ["style_clash"]

    # Edit one item's color to a neutral so the same pair now harmonizes → the
    # re-derived type is pairs_well, NOT style_clash.
    bottom.color_main = "white"
    await db_session.flush()
    await repo.delete_rule_based_for_user(user.id)
    await repo.generate_rule_based([top, bottom])
    await db_session.flush()

    rule_rows = list(
        await db_session.scalars(
            select(WardrobeItemRelationship).where(
                WardrobeItemRelationship.source == "rule_based"
            )
        )
    )
    # Only the freshly derived edge remains — the stale style_clash row is gone.
    assert [row.relationship_type for row in rule_rows] == ["pairs_well"]

    # The user_feedback edge is untouched.
    surviving = await db_session.get(WardrobeItemRelationship, feedback.id)
    assert surviving is not None
    assert surviving.source == "user_feedback"
    assert surviving.relationship_type == "user_approved_pairing"
