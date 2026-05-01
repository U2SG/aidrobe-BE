import uuid

from app.application.recommendations.background import schedule_today_recompute
from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.infrastructure.persistence.repositories.wardrobe_repo import SqlAlchemyWardrobeRepository


class DeleteWardrobeItem:
    def __init__(self, repository: SqlAlchemyWardrobeRepository) -> None:
        self.repository = repository

    async def execute(self, item_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        item = await self.repository.get_for_user(item_id, user_id)
        if item is None:
            raise FriendlyException(
                error_codes.NOT_FOUND, "Wardrobe item not found", status_code=404
            )
        await self.repository.soft_delete(item)
        await schedule_today_recompute(user_id)
        return True
