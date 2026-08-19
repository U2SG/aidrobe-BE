"""V3-024 acceptance 5 — feedback is removed with its user/asset lifecycle.

Three deletion paths reach this table and each removes the row by a different
mechanism, so each is checked rather than inferred from the FK declarations:

- `DELETE /v1/avatar-profile?purge_derived=true` → `delete_static_tryon_for_user`
  issues a **bulk Core DELETE** over `outfit_visual_assets`. No ORM cascade
  observes that, so only the database-level `ON DELETE CASCADE` on `asset_id`
  removes the feedback with it.
- `clear_user_data` (BE#83) sweeps every table with a direct `users` FK. The
  `user_id` column is what puts this table in that sweep by default; without
  it, the table would be reachable only indirectly and would drop out if the
  asset path ever changed.
- Account purge deletes the `users` row, and both FKs cascade.

The mirror case matters too: `purge_derived=false` must **keep** the render,
and therefore its feedback — the parameter would be decorative otherwise
(contract §4.2, the reasoning in `delete_avatar`).
"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.users.clear_data import clear_user_data
from app.common.enums import VisualType
from app.infrastructure.persistence.models.outfit_visual import OutfitVisualAsset
from app.infrastructure.persistence.models.preview_feedback import StaticPreviewFeedback
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.repositories.outfit_visual_repo import (
    SqlAlchemyOutfitVisualRepository,
)


async def _seed_feedback(
    session: AsyncSession,
    *,
    visual_type: str = VisualType.STATIC_TRYON.value,
) -> tuple[uuid.UUID, uuid.UUID]:
    user_id = uuid.uuid4()
    session.add(User(id=user_id))
    await session.flush()
    is_tryon = visual_type == VisualType.STATIC_TRYON.value
    asset = OutfitVisualAsset(
        user_id=user_id,
        visual_type=visual_type,
        generation_status="ready",
        provider="fake-static-tryon" if is_tryon else None,
        model_version="compositor-1" if is_tryon else None,
        prompt_version="p0-static-1" if is_tryon else None,
    )
    session.add(asset)
    await session.flush()
    session.add(
        StaticPreviewFeedback(user_id=user_id, asset_id=asset.id, verdict="looks_like_me")
    )
    await session.flush()
    return user_id, asset.id


async def _feedback_count(session: AsyncSession, user_id: uuid.UUID) -> int:
    return (
        await session.scalar(
            select(func.count())
            .select_from(StaticPreviewFeedback)
            .where(StaticPreviewFeedback.user_id == user_id)
        )
    ) or 0


async def test_purge_derived_removes_the_feedback_with_the_render(
    db_session: AsyncSession,
) -> None:
    user_id, _asset_id = await _seed_feedback(db_session)
    assert await _feedback_count(db_session, user_id) == 1

    await SqlAlchemyOutfitVisualRepository(db_session).delete_static_tryon_for_user(user_id)

    assert await _feedback_count(db_session, user_id) == 0


async def test_a_board_render_is_untouched_by_the_try_on_sweep(
    db_session: AsyncSession,
) -> None:
    """The sweep is scoped to `static_tryon`. An M10 board cannot carry
    feedback (the endpoint 404s for it), but the row-level guarantee is that
    the sweep does not reach past its own visual type."""
    user_id, asset_id = await _seed_feedback(
        db_session, visual_type=VisualType.OUTFIT_BOARD.value
    )

    await SqlAlchemyOutfitVisualRepository(db_session).delete_static_tryon_for_user(user_id)

    assert await db_session.get(OutfitVisualAsset, asset_id) is not None
    assert await _feedback_count(db_session, user_id) == 1


async def test_clear_user_data_removes_the_feedback(db_session: AsyncSession) -> None:
    """BE#83's sweep is metadata-driven over columns with a `users` FK, so this
    table is covered by default — the privacy-safe direction."""
    user_id, _asset_id = await _seed_feedback(db_session)

    await clear_user_data(db_session, user_id)

    assert await _feedback_count(db_session, user_id) == 0
    assert await db_session.get(User, user_id) is not None


async def test_deleting_the_user_cascades(db_session: AsyncSession) -> None:
    user_id, _asset_id = await _seed_feedback(db_session)

    await db_session.execute(delete(User).where(User.id == user_id))
    await db_session.flush()

    assert await _feedback_count(db_session, user_id) == 0


async def test_another_users_feedback_survives_all_of_it(db_session: AsyncSession) -> None:
    """Every deletion above is user-scoped; a bystander's record is untouched."""
    victim_id, _victim_asset = await _seed_feedback(db_session)
    bystander_id, _bystander_asset = await _seed_feedback(db_session)

    await SqlAlchemyOutfitVisualRepository(db_session).delete_static_tryon_for_user(victim_id)
    await clear_user_data(db_session, victim_id)

    assert await _feedback_count(db_session, victim_id) == 0
    assert await _feedback_count(db_session, bystander_id) == 1
