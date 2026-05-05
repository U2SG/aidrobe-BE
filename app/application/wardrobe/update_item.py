import uuid

from app.application.recommendations.background import schedule_today_recompute
from app.common import error_codes
from app.common.enums import ItemStatus
from app.common.exceptions import FriendlyException
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)
from app.infrastructure.persistence.repositories.style_repo import SqlAlchemyStyleRepository
from app.infrastructure.persistence.repositories.wardrobe_repo import SqlAlchemyWardrobeRepository


class UpdateWardrobeItem:
    def __init__(
        self,
        repository: SqlAlchemyWardrobeRepository,
        recommendation_repository: SqlAlchemyRecommendationRepository | None = None,
        style_repository: SqlAlchemyStyleRepository | None = None,
    ) -> None:
        self.repository = repository
        self.recommendation_repository = recommendation_repository
        self.style_repository = style_repository

    async def execute(
        self,
        item_id: uuid.UUID,
        user_id: uuid.UUID,
        title: str | None = None,
        category: str | None = None,
        color_main: str | None = None,
        status: str | None = None,
        location: str | None = None,
    ) -> WardrobeItem:
        item = await self.repository.get_for_user(item_id, user_id)
        if item is None:
            raise FriendlyException(
                error_codes.NOT_FOUND, "Wardrobe item not found", status_code=404
            )
        if status is not None and status not in {
            ItemStatus.WEARABLE.value,
            ItemStatus.UNAVAILABLE.value,
        }:
            raise FriendlyException(
                error_codes.VALIDATION_ERROR, "Invalid item status", status_code=422
            )

        if title is not None:
            item.title = title
        if category is not None:
            item.category = category
        if color_main is not None:
            item.color_main = color_main
        if status is not None:
            item.status = status
        if location is not None:
            item.location = location
        await self.repository.flush()
        await schedule_today_recompute(user_id, self.recommendation_repository, self.style_repository)
        return item
