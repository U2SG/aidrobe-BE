import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.outfits.replacements import find_replacement_candidates
from app.common.enums import ItemStatus
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
    title: str,
    category: str = "top",
    color: str = "white",
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


async def test_find_replacement_candidates_returns_ranked_same_category(
    db_session: AsyncSession,
) -> None:
    user_id = await _seed_user(db_session)
    target = await _add_item(db_session, user_id, title="target", color="white")
    same_color = await _add_item(db_session, user_id, title="same color", color="white")
    other_color = await _add_item(db_session, user_id, title="other color", color="blue")
    await _add_item(db_session, user_id, title="different category", category="bottom")

    candidates = await find_replacement_candidates(db_session, target, k=8)

    assert [candidate.item.id for candidate in candidates] == [same_color.id, other_color.id]
    assert [candidate.similarity for candidate in candidates] == [0.9, 0.6]


async def test_find_replacement_candidates_excludes_self_peers_and_foreign_items(
    db_session: AsyncSession,
) -> None:
    user_id = await _seed_user(db_session)
    foreign_user_id = await _seed_user(db_session)
    target = await _add_item(db_session, user_id, title="target")
    peer = await _add_item(db_session, user_id, title="peer")
    owned = await _add_item(db_session, user_id, title="owned")
    await _add_item(db_session, foreign_user_id, title="foreign")

    candidates = await find_replacement_candidates(
        db_session,
        target,
        exclude_ids=[peer.id],
        k=8,
    )

    assert [candidate.item.id for candidate in candidates] == [owned.id]

