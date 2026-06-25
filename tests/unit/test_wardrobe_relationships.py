import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import Category, ItemStatus
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
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
