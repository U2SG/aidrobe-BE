import uuid
from datetime import UTC, datetime

from app.application.recommendations.background import schedule_today_recompute
from app.common import error_codes
from app.common.enums import ImageType, ImportStatus, ItemStatus
from app.common.exceptions import FriendlyException
from app.infrastructure.persistence.models.wardrobe import WardrobeItem, WardrobeItemImage
from app.infrastructure.persistence.repositories.import_task_repo import (
    SqlAlchemyImportTaskRepository,
)
from app.infrastructure.persistence.repositories.wardrobe_repo import SqlAlchemyWardrobeRepository


class ConfirmImageImportTask:
    def __init__(
        self,
        import_task_repository: SqlAlchemyImportTaskRepository,
        wardrobe_repository: SqlAlchemyWardrobeRepository,
    ) -> None:
        self.import_task_repository = import_task_repository
        self.wardrobe_repository = wardrobe_repository

    async def execute(
        self,
        task_id: uuid.UUID,
        user_id: uuid.UUID,
        title: str,
        category: str,
        color_main: str,
    ) -> WardrobeItem:
        task = await self.import_task_repository.get_for_user(task_id, user_id)
        if task is None:
            raise FriendlyException(error_codes.NOT_FOUND, "Import task not found", status_code=404)
        if task.task_status != ImportStatus.WAITING_CONFIRM.value:
            raise FriendlyException(
                error_codes.STATE_CONFLICT,
                "Import task is not waiting for confirmation",
                status_code=409,
            )

        original_image_url = None
        if task.candidate_payload:
            original_image_url = task.candidate_payload.get("original_image_url")
        if original_image_url is None:
            original_image_url = task.input_payload.get("image_url")

        item = WardrobeItem(
            user_id=user_id,
            title=title,
            category=category,
            color_main=color_main,
            status=ItemStatus.WEARABLE.value,
            cover_image_url=original_image_url,
            original_image_url=original_image_url,
        )
        await self.wardrobe_repository.add(item)

        if original_image_url:
            now = datetime.now(UTC)
            await self.wardrobe_repository.add_image(
                WardrobeItemImage(
                    item_id=item.id,
                    image_type=ImageType.ORIGINAL.value,
                    image_url=original_image_url,
                    created_at=now,
                )
            )
            await self.wardrobe_repository.add_image(
                WardrobeItemImage(
                    item_id=item.id,
                    image_type=ImageType.STANDARD.value,
                    image_url=original_image_url,
                    created_at=now,
                )
            )

        task.task_status = ImportStatus.COMPLETED.value
        await self.import_task_repository.flush()
        await schedule_today_recompute(user_id)
        return item
