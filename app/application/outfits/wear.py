import uuid
from datetime import UTC, date, datetime

from sqlalchemy import func, update

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
        # Contract: get_outfit_for_user MUST eagerly load `outfit.items`
        # (selectinload). Lazy-load here would raise MissingGreenlet under async.
        outfit = await self.repository.get_outfit_for_user(outfit_id, user_id)
        if outfit is None:
            raise FriendlyException(error_codes.NOT_FOUND, "Outfit not found", status_code=404)

        now = datetime.now(UTC)
        wear_history, inserted = await self.repository.insert_wear_history_idempotent(
            WearHistory(
                user_id=user_id,
                outfit_id=outfit_id,
                selected_at=now,
                worn_date=worn_date,
                created_at=now,
            )
        )

        if not inserted:
            return wear_history

        item_ids = [oi.item_id for oi in outfit.items]
        if item_ids:
            # Single round-trip: bump wear_count and forward-only update last_worn_at.
            await self.repository.session.execute(
                update(WardrobeItem)
                .where(WardrobeItem.id.in_(item_ids))
                .values(
                    wear_count=WardrobeItem.wear_count + 1,
                    last_worn_at=func.greatest(
                        func.coalesce(WardrobeItem.last_worn_at, worn_date),
                        worn_date,
                    ),
                )
            )

        return wear_history
