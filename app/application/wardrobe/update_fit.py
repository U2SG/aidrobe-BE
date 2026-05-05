import uuid
from collections.abc import Mapping
from typing import cast

from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.wardrobe_repo import SqlAlchemyWardrobeRepository

FitUpdateValue = str | int | None


class UpdateWardrobeFit:
    def __init__(self, repository: SqlAlchemyWardrobeRepository) -> None:
        self.repository = repository

    async def execute(
        self,
        item_id: uuid.UUID,
        user_id: uuid.UUID,
        updates: Mapping[str, FitUpdateValue],
    ) -> WardrobeItem:
        item = await self.repository.get_for_user(item_id, user_id)
        if item is None:
            raise FriendlyException(
                error_codes.NOT_FOUND, "Wardrobe item not found", status_code=404
            )

        if "size_label" in updates:
            item.size_label = cast(str | None, updates["size_label"])
        if "size_region" in updates:
            item.size_region = cast(str | None, updates["size_region"])
        if "fit_intent" in updates:
            item.fit_intent = cast(str | None, updates["fit_intent"])
        if "fit_status" in updates:
            fit_status = updates["fit_status"]
            if fit_status is None:
                raise FriendlyException(
                    error_codes.VALIDATION_ERROR, "fit_status cannot be null", status_code=422
                )
            item.fit_status = cast(str, fit_status)
        if "comfort_score" in updates:
            item.comfort_score = cast(int | None, updates["comfort_score"])

        await self.repository.flush()
        return item
