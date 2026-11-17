import uuid

from sqlalchemy import select

from app.application.ask.styling import fingerprint
from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.common.local_time import utc_now
from app.infrastructure.persistence.models.ask_conversation import AskConversation, AskMessage
from app.infrastructure.persistence.models.ask_session import AskSession
from app.infrastructure.persistence.repositories.preference_memory_repo import SqlAlchemyPreferenceMemoryRepository
from app.infrastructure.persistence.repositories.style_repo import SqlAlchemyStyleRepository


class DeleteStyleInspiration:
    def __init__(self, repository: SqlAlchemyStyleRepository) -> None:
        self.repository = repository

    async def execute(self, profile_id: uuid.UUID, user_id: uuid.UUID) -> None:
        memory = SqlAlchemyPreferenceMemoryRepository(self.repository.session)
        user = await memory.lock_user(user_id)
        deleted = await self.repository.delete(profile_id, user_id)
        if not deleted:
            raise FriendlyException(
                error_codes.NOT_FOUND, "Inspiration not found", status_code=404
            )
        await self._clear_initial_inferred_style(profile_id, user_id)
        await memory.reset_context(user, utc_now())
        await self.repository.session.flush()

    async def _clear_initial_inferred_style(self, profile_id: uuid.UUID, user_id: uuid.UUID) -> None:
        # Older quick starts copied photo tags into target_style without recording their origin.
        rows = await self.repository.session.execute(
            select(AskConversation, AskSession)
            .select_from(AskConversation)
            .join(AskMessage, AskMessage.conversation_id == AskConversation.id)
            .join(AskSession, AskSession.id == AskMessage.ask_session_id)
            .where(
                AskConversation.user_id == user_id, AskSession.user_id == user_id,
                AskConversation.styling_revision == 1, AskConversation.message_count == 2,
                AskConversation.design_constraints["inspiration_id"].astext == str(profile_id),
                AskMessage.role == "user",
            )
            .with_for_update(of=AskConversation)
            .execution_options(populate_existing=True)
        )
        for conversation, request in rows:
            constraints = conversation.design_constraints or {}
            if not constraints.get("target_style"):
                continue
            # Exact hashes prove the supported legacy clients supplied no constraints; equality of tags cannot.
            inferred = any(request.request_fingerprint == fingerprint(
                "ask", question=request.question, context=None, locale=locale, focused_item_id=None,
                conversation_id=None, expected_revision=None, base_outfit_id=None, constraints=None,
                inspiration_id=profile_id, inspiration_only=True,
            ) for locale in (None, "zh-CN"))
            if inferred:
                conversation.design_constraints = {**constraints, "target_style": None}
                conversation.styling_revision += 1
