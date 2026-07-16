import uuid

from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.wardrobe_repo import SqlAlchemyWardrobeRepository


class ListWardrobeItems:
    def __init__(self, repository: SqlAlchemyWardrobeRepository) -> None:
        self.repository = repository

    async def execute(
        self,
        user_id: uuid.UUID,
        category: str | None,
        limit: int,
        offset: int,
        query: str | None = None,
        status: str | None = None,
        season: str | None = None,
        color: str | None = None,
    ) -> tuple[list[WardrobeItem], int]:
        return await self.repository.list_for_user(
            user_id,
            category,
            query=query,
            status=status,
            season=season,
            color=color,
            limit=limit,
            offset=offset,
        )
