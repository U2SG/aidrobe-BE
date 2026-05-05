import uuid

from app.infrastructure.persistence.models.style import UserStyleProfile
from app.infrastructure.persistence.repositories.style_repo import SqlAlchemyStyleRepository


class ListStyleInspirations:
    def __init__(self, repository: SqlAlchemyStyleRepository) -> None:
        self.repository = repository

    async def execute(
        self, user_id: uuid.UUID, limit: int = 20, offset: int = 0
    ) -> list[UserStyleProfile]:
        return await self.repository.list_inspirations(user_id, limit, offset)
