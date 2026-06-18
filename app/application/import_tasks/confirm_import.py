import uuid
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation

from app.application.cost_guard import assert_within_quota
from app.application.recommendations.background import schedule_today_recompute
from app.common import error_codes
from app.common.enums import CutoutStatus, ImageType, ImportStatus, ItemStatus
from app.common.exceptions import FriendlyException
from app.config.settings import get_settings
from app.infrastructure.persistence.models.wardrobe import WardrobeItem, WardrobeItemImage
from app.infrastructure.persistence.repositories.import_task_repo import (
    SqlAlchemyImportTaskRepository,
)
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)
from app.infrastructure.persistence.repositories.style_repo import SqlAlchemyStyleRepository
from app.infrastructure.persistence.repositories.wardrobe_repo import SqlAlchemyWardrobeRepository
from app.infrastructure.workers.cutout_worker import enqueue_item_cutout


class ConfirmImageImportTask:
    def __init__(
        self,
        import_task_repository: SqlAlchemyImportTaskRepository,
        wardrobe_repository: SqlAlchemyWardrobeRepository,
        recommendation_repository: SqlAlchemyRecommendationRepository | None = None,
        style_repository: SqlAlchemyStyleRepository | None = None,
    ) -> None:
        self.import_task_repository = import_task_repository
        self.wardrobe_repository = wardrobe_repository
        self.recommendation_repository = recommendation_repository
        self.style_repository = style_repository

    async def execute(
        self,
        task_id: uuid.UUID,
        user_id: uuid.UUID,
        title: str,
        category: str,
        color_main: str,
        location: str | None = None,
        size_label: str | None = None,
        fit_status: str | None = None,
        purchase_price: Decimal | None = None,
        purchase_date: date | None = None,
    ) -> WardrobeItem:
        task = await self.import_task_repository.get_for_user(task_id, user_id, for_update=True)
        if task is None:
            raise FriendlyException(error_codes.NOT_FOUND, "Import task not found", status_code=404)
        if task.task_status != ImportStatus.WAITING_CONFIRM.value:
            raise FriendlyException(
                error_codes.STATE_CONFLICT,
                "Import task is not waiting for confirmation",
                status_code=409,
            )

        candidate = task.candidate_payload or {}
        original_image_url = candidate.get("original_image_url")
        if original_image_url is None:
            original_image_url = task.input_payload.get("image_url")

        # M18 — auto-fill from the OCR candidate when the client didn't override.
        # candidate_payload stores price/date as strings (JSONB-safe), so coerce.
        if size_label is None:
            size_label = candidate.get("size_label")
        if purchase_price is None:
            purchase_price = _to_decimal(candidate.get("purchase_price"))
        if purchase_date is None:
            purchase_date = _to_date(candidate.get("purchase_date"))

        item = WardrobeItem(
            user_id=user_id,
            title=title,
            category=category,
            color_main=color_main,
            status=ItemStatus.WEARABLE.value,
            cover_image_url=original_image_url,
            original_image_url=original_image_url,
            location=location,
            size_label=size_label,
            fit_status=fit_status or "unknown",
            purchase_price=purchase_price,
            purchase_date=purchase_date,
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

        # V4 — kick off the async product-shot cutout. Best-effort: a blown
        # daily quota just leaves the item on its original photo (skipped),
        # never blocks the import. Gated by the flag; data already persisted.
        if get_settings().ENABLE_ITEM_CUTOUT and original_image_url:
            session = self.wardrobe_repository.session
            try:
                await assert_within_quota(session, user_id, "cutout")
                item.cutout_status = CutoutStatus.QUEUED.value
                await enqueue_item_cutout(
                    session,
                    item_id=item.id,
                    user_id=user_id,
                    source_url=original_image_url,
                    category=category,
                )
            except FriendlyException:
                # quota exhausted (429) — leave cutout_status='skipped'.
                pass

        task.task_status = ImportStatus.COMPLETED.value
        await self.import_task_repository.flush()
        await schedule_today_recompute(user_id, self.recommendation_repository, self.style_repository)
        return item


def _to_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _to_date(value: object) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        return None
