import uuid
from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.persistence.models.ask_conversation import (
    AskConversation,
    AskConversationState,
    AskMessage,
)


class SqlAlchemyAskConversationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create_conversation(
        self, *, user_id: uuid.UUID, title: str | None = None
    ) -> AskConversation:
        row = AskConversation(user_id=user_id, title=title)
        self.session.add(row)
        await self.session.flush()
        return row

    async def get_owned_conversation(
        self, user_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> AskConversation | None:
        return cast(
            AskConversation | None,
            await self.session.scalar(
                select(AskConversation).where(
                    AskConversation.id == conversation_id,
                    AskConversation.user_id == user_id,
                )
            ),
        )

    async def get_state(
        self, user_id: uuid.UUID, conversation_id: uuid.UUID
    ) -> AskConversationState | None:
        return cast(
            AskConversationState | None,
            await self.session.scalar(
                select(AskConversationState).where(
                    AskConversationState.conversation_id == conversation_id,
                    AskConversationState.user_id == user_id,
                )
            ),
        )

    async def append_message(
        self,
        *,
        conversation: AskConversation,
        role: str,
        content: str,
        ask_session_id: uuid.UUID | None = None,
        message_metadata: dict[str, Any] | None = None,
    ) -> AskMessage:
        row = AskMessage(
            conversation_id=conversation.id,
            user_id=conversation.user_id,
            ask_session_id=ask_session_id,
            role=role,
            content=content,
            message_metadata=message_metadata,
        )
        # Bumping message_count dirties the conversation row, so its
        # `onupdate=func.now()` fires — no manual updated_at needed. No per-row
        # flush: get_db_session commits on yield-exit.
        conversation.message_count += 1
        self.session.add(row)
        return row

    async def list_messages(
        self,
        user_id: uuid.UUID,
        conversation_id: uuid.UUID,
        *,
        limit: int | None = None,
    ) -> list[AskMessage]:
        stmt = (
            select(AskMessage)
            .where(
                AskMessage.user_id == user_id,
                AskMessage.conversation_id == conversation_id,
            )
            .order_by(AskMessage.created_at.asc(), AskMessage.id.asc())
        )
        if limit is not None:
            stmt = stmt.limit(limit)
        result = await self.session.scalars(stmt)
        return list(result)

    async def upsert_state(
        self,
        *,
        conversation: AskConversation,
        state: AskConversationState | None,
        last_outfit: dict[str, Any] | None,
        active_constraints: Sequence[str],
        rejected_item_ids: Sequence[str],
        preferred_item_ids: Sequence[str],
        summary: str | None,
    ) -> AskConversationState:
        # `state` is the row the caller already loaded via `get_state` — reuse it
        # rather than re-SELECT. None means no state row exists yet for this
        # conversation, so create one.
        row = state
        if row is None:
            row = AskConversationState(
                conversation_id=conversation.id,
                user_id=conversation.user_id,
            )
            self.session.add(row)
        row.last_outfit = last_outfit
        row.active_constraints = list(active_constraints) or None
        row.rejected_item_ids = list(rejected_item_ids) or None
        row.preferred_item_ids = list(preferred_item_ids) or None
        row.summary = summary
        # Assigning the columns above dirties the row, so its
        # `onupdate=func.now()` fires — no manual updated_at needed.
        await self.session.flush()
        return row
