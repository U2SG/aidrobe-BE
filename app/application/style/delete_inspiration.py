import uuid

from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.common.local_time import utc_now
from app.infrastructure.persistence.repositories.preference_memory_repo import SqlAlchemyPreferenceMemoryRepository
from app.infrastructure.persistence.repositories.style_repo import SqlAlchemyStyleRepository


class DeleteStyleInspiration:
    def __init__(self, repository: SqlAlchemyStyleRepository) -> None:
        self.repository = repository

    async def execute(self, profile_id: uuid.UUID, user_id: uuid.UUID) -> None:
        if await self.repository.get_by_id(profile_id, user_id) is None:
            raise FriendlyException(error_codes.NOT_FOUND, "Inspiration not found", status_code=404)
        memory = SqlAlchemyPreferenceMemoryRepository(self.repository.session)
        user = await memory.lock_user(user_id)
        deleted = await self.repository.delete(profile_id, user_id)
        if not deleted:
            raise FriendlyException(
                error_codes.NOT_FOUND, "Inspiration not found", status_code=404
            )
        await memory.reset_context(user, utc_now())
        await self.repository.session.flush()
