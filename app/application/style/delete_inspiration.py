import uuid

from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.infrastructure.persistence.repositories.style_repo import SqlAlchemyStyleRepository


class DeleteStyleInspiration:
    def __init__(self, repository: SqlAlchemyStyleRepository) -> None:
        self.repository = repository

    async def execute(self, profile_id: uuid.UUID, user_id: uuid.UUID) -> None:
        deleted = await self.repository.delete(profile_id, user_id)
        if not deleted:
            raise FriendlyException(
                error_codes.NOT_FOUND, "Inspiration not found", status_code=404
            )
