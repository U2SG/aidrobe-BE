"""V3-003 — repositories for purchase candidates and decision snapshots.

`SqlAlchemyWardrobeDecisionRepository` deliberately has **no update and no
delete method**: a decision snapshot is written once and replayed forever
(V3_SPEC §11.2). Reads scope every query by `user_id` so a wrong-owner lookup
returns None and the caller can answer 404 without leaking existence
(contract §3).
"""

import uuid
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.persistence.models.purchase_decision import (
    PurchaseCandidate,
    WardrobeDecision,
)


class SqlAlchemyPurchaseCandidateRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, candidate: PurchaseCandidate) -> PurchaseCandidate:
        self.session.add(candidate)
        await self.session.flush()
        return candidate

    async def get_for_user(self, candidate_id: uuid.UUID, user_id: uuid.UUID) -> PurchaseCandidate | None:
        return cast(
            PurchaseCandidate | None,
            await self.session.scalar(
                select(PurchaseCandidate).where(
                    PurchaseCandidate.id == candidate_id,
                    PurchaseCandidate.user_id == user_id,
                )
            ),
        )

    async def list_for_user(self, user_id: uuid.UUID) -> list[PurchaseCandidate]:
        result = await self.session.scalars(
            select(PurchaseCandidate)
            .where(PurchaseCandidate.user_id == user_id)
            .order_by(PurchaseCandidate.created_at.desc())
        )
        return list(result)


class SqlAlchemyWardrobeDecisionRepository:
    """Append-only. Adding an update path here would break the immutability
    guarantee the whole table exists for."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, decision: WardrobeDecision) -> WardrobeDecision:
        self.session.add(decision)
        await self.session.flush()
        return decision

    async def get_for_user(self, decision_id: uuid.UUID, user_id: uuid.UUID) -> WardrobeDecision | None:
        return cast(
            WardrobeDecision | None,
            await self.session.scalar(
                select(WardrobeDecision).where(
                    WardrobeDecision.id == decision_id,
                    WardrobeDecision.user_id == user_id,
                )
            ),
        )

    async def get_for_candidate(self, candidate_id: uuid.UUID, user_id: uuid.UUID) -> WardrobeDecision | None:
        """The existing decision for a candidate, if any — the idempotent read
        behind "repeated calls return the existing decision" (contract §5.3)."""
        return cast(
            WardrobeDecision | None,
            await self.session.scalar(
                select(WardrobeDecision).where(
                    WardrobeDecision.candidate_id == candidate_id,
                    WardrobeDecision.user_id == user_id,
                )
            ),
        )
