import uuid
from datetime import UTC, date, datetime

from sqlalchemy import update

from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.infrastructure.persistence.models.recommendation import WearHistory
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)


class WearOutfit:
    def __init__(self, repository: SqlAlchemyRecommendationRepository) -> None:
        self.repository = repository

    async def execute(
        self, outfit_id: uuid.UUID, user_id: uuid.UUID, worn_date: date
    ) -> WearHistory:
        outfit = await self.repository.get_outfit_for_user(outfit_id, user_id)
        if outfit is None:
            raise FriendlyException(error_codes.NOT_FOUND, "Outfit not found", status_code=404)

        existing = await self.repository.get_wear_history(outfit_id, user_id, worn_date)
        if existing is not None:
            return existing

        wear_history = await self.repository.add_wear_history(
            WearHistory(
                user_id=user_id,
                outfit_id=outfit_id,
                selected_at=datetime.now(UTC),
                worn_date=worn_date,
                created_at=datetime.now(UTC),
            )
        )

        # Increment wear_count for every wardrobe item in this outfit
        item_ids = [oi.item_id for oi in outfit.items]
        if item_ids:
            await self.repository.session.execute(
                update(WardrobeItem)
                .where(WardrobeItem.id.in_(item_ids))
                .values(wear_count=WardrobeItem.wear_count + 1)
            )

        return wear_history
