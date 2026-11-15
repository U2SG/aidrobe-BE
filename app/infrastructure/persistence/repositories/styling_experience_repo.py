"""Owned experience reads; withdrawn requests remain as idempotency tombstones."""

import uuid
from datetime import date, datetime
from typing import cast

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.persistence.models.recommendation import WearHistory
from app.infrastructure.persistence.models.styling_experience import StylingExperience
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.repositories.preference_memory_repo import SqlAlchemyPreferenceMemoryRepository


class SqlAlchemyStylingExperienceRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_owned(self, user_id: uuid.UUID, experience_id: uuid.UUID) -> StylingExperience | None:
        return cast(StylingExperience | None, await self.session.scalar(
            select(StylingExperience).where(
                StylingExperience.user_id == user_id, StylingExperience.id == experience_id
            ).execution_options(populate_existing=True)
        ))

    async def by_request(self, user_id: uuid.UUID, request_id: uuid.UUID) -> StylingExperience | None:
        return cast(StylingExperience | None, await self.session.scalar(
            select(StylingExperience).where(
                StylingExperience.user_id == user_id, StylingExperience.request_id == request_id
            ).execution_options(populate_existing=True)
        ))

    async def list_live(
        self, user_id: uuid.UUID, *, outfit_id: uuid.UUID | None = None,
        after: datetime | None = None, limit: int = 20,
    ) -> list[StylingExperience]:
        stmt = select(StylingExperience).where(
            StylingExperience.user_id == user_id, StylingExperience.deleted_at.is_(None)
        )
        if outfit_id is not None:
            stmt = stmt.where(StylingExperience.outfit_id == outfit_id)
        if after is not None:
            stmt = stmt.where(StylingExperience.created_at > after)
        if outfit_id is not None:
            # Preserve the two current intent states when many dated wear
            # reviews would otherwise push them out of the first page.
            stmt = stmt.order_by(case((StylingExperience.kind == "wear_feedback", 1), else_=0))
        rows = list(await self.session.scalars(
            stmt.order_by(StylingExperience.created_at.desc(), StylingExperience.id.desc()).limit(limit)
        ))
        return sorted(rows, key=lambda row: (row.created_at, row.id), reverse=True)

    async def current(
        self, user_id: uuid.UUID, outfit_id: uuid.UUID, kind: str, worn_date: date | None,
    ) -> StylingExperience | None:
        return cast(StylingExperience | None, await self.session.scalar(select(StylingExperience).where(
            StylingExperience.user_id == user_id, StylingExperience.outfit_id == outfit_id,
            StylingExperience.kind == kind, StylingExperience.worn_date == worn_date,
            StylingExperience.deleted_at.is_(None),
        )))

    async def latest_worn_date(self, user_id: uuid.UUID, outfit_id: uuid.UUID, *, through: date) -> date | None:
        return await self.session.scalar(select(func.max(WearHistory.worn_date)).where(
            WearHistory.user_id == user_id, WearHistory.outfit_id == outfit_id, WearHistory.worn_date <= through,
        ))

    async def reset_context(self, user: User, at: datetime) -> None:
        await SqlAlchemyPreferenceMemoryRepository(self.session).reset_context(user, at)
