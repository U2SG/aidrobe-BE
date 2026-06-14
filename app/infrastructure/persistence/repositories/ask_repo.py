import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.persistence.models.ask_session import AskSession


class SqlAlchemyAskRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        user_id: uuid.UUID,
        question: str,
        answer: str | None,
        item_refs: list[str],
        context: dict[str, Any] | None,
        tokens_used: int | None,
    ) -> AskSession:
        row = AskSession(
            user_id=user_id,
            question=question,
            answer=answer,
            item_refs=item_refs,
            context=context,
            tokens_used=tokens_used,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def list_for_user(
        self, user_id: uuid.UUID, *, limit: int, offset: int
    ) -> list[AskSession]:
        result = await self.session.scalars(
            select(AskSession)
            .where(AskSession.user_id == user_id)
            .order_by(AskSession.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result)
