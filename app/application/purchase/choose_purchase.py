"""Persist an owned decision's user intention without changing its evidence."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.common.local_time import utc_now
from app.infrastructure.persistence.models.purchase_decision import WardrobeDecision, WardrobeDecisionChoice
from app.infrastructure.persistence.repositories.preference_memory_repo import SqlAlchemyPreferenceMemoryRepository


async def choose_purchase(
    session: AsyncSession, user_id: uuid.UUID, decision_id: uuid.UUID, choice: str | None,
    *, ask_session_id: uuid.UUID | None = None,
) -> WardrobeDecision:
    if choice not in (None, "buy", "skip", "later"):
        raise FriendlyException(error_codes.VALIDATION_ERROR, "Invalid purchase choice", 422)
    # Same lock order as design/forget. The decision lock also serializes a
    # first choice insert with another device without modifying the decision.
    await SqlAlchemyPreferenceMemoryRepository(session).lock_user(user_id)
    decision = await session.scalar(select(WardrobeDecision).where(
        WardrobeDecision.id == decision_id, WardrobeDecision.user_id == user_id,
    ).with_for_update())
    if decision is None:
        raise FriendlyException(error_codes.NOT_FOUND, "Wardrobe decision not found", 404)
    row = await session.scalar(select(WardrobeDecisionChoice).where(
        WardrobeDecisionChoice.decision_id == decision_id, WardrobeDecisionChoice.user_id == user_id,
    ).execution_options(populate_existing=True))
    if row is not None and row.choice == choice:
        return decision  # A repeated PUT preserves the action's original timestamp/provenance.
    if row is None:
        row = WardrobeDecisionChoice(decision_id=decision_id, user_id=user_id)
        session.add(row)
    row.choice = choice
    row.source = "ask" if ask_session_id is not None else "button"
    row.ask_session_id = ask_session_id
    row.updated_at = utc_now()
    await session.flush()
    return decision
