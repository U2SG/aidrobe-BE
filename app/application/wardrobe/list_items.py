import uuid

from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.wardrobe_repo import SqlAlchemyWardrobeRepository


class ListWardrobeItems:
    def __init__(self, repository: SqlAlchemyWardrobeRepository) -> None:
        self.repository = repository

    async def execute(
        self, user_id: uuid.UUID, category: str | None, limit: int, offset: int
    ) -> tuple[list[WardrobeItem], int]:
        return await self.repository.list_for_user(user_id, category, limit, offset)
