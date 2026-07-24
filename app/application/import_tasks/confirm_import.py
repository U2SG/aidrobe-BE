import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from app.application.cost_guard import assert_within_quota
from app.application.recommendations.background import schedule_today_recompute
from app.common import error_codes
from app.common.enums import CutoutStatus, ImageType, ImportStatus, ItemStatus
from app.common.exceptions import FriendlyException
from app.config.settings import get_settings
from app.infrastructure.persistence.models.import_task import ImportTask
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


@dataclass(frozen=True)
class ConfirmItemInput:
    """One candidate the client elected to import, with its edited fields.

    Layer-neutral input for `confirm_items`; the router maps its request schema
    onto this so the application layer doesn't import presentation types.
    """

    temp_item_id: str
    title: str
    category: str
    color_main: str
    location: str | None = None
    size_label: str | None = None
    fit_status: str | None = None
    fit_intent: str | None = None


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
        fit_intent: str | None = None,
        purchase_price: Decimal | None = None,
        purchase_date: date | None = None,
    ) -> WardrobeItem:
        task = await self._load_waiting_task(task_id, user_id)

        item = await self._create_item(
            user_id=user_id,
            candidate=task.candidate_payload or {},
            fallback_image_url=task.input_payload.get("image_url"),
            title=title,
            category=category,
            color_main=color_main,
            location=location,
            size_label=size_label,
            fit_status=fit_status,
            fit_intent=fit_intent,
            purchase_price=purchase_price,
            purchase_date=purchase_date,
        )

        task.task_status = ImportStatus.COMPLETED.value
        await self.import_task_repository.flush()
        await schedule_today_recompute(user_id, self.recommendation_repository, self.style_repository)
        return item

    async def confirm_items(
        self,
        task_id: uuid.UUID,
        user_id: uuid.UUID,
        items: list[ConfirmItemInput],
    ) -> list[tuple[str, WardrobeItem]]:
        """BE#25 — batch-confirm a subset of the task's candidates.

        Each input targets a candidate by `temp_item_id`; the typed form fields
        win over the candidate's AI guesses (same provenance rules as single
        confirm). Reuses `_create_item` so item/image/cutout behavior is shared.
        """
        task = await self._load_waiting_task(task_id, user_id)

        # Build temp_item_id -> raw candidate map from the persisted payload.
        # Legacy rows may carry a None temp_item_id; those keys are unaddressable
        # so we skip them when mapping.
        candidate_payload = task.candidate_payload or {}
        raw_items = candidate_payload.get("candidate_items")
        candidate_by_temp_id: dict[str, dict[str, Any]] = {}
        if isinstance(raw_items, list):
            for raw in raw_items:
                if not isinstance(raw, dict):
                    continue
                temp_id = raw.get("temp_item_id")
                if isinstance(temp_id, str):
                    candidate_by_temp_id[temp_id] = raw

        seen: set[str] = set()
        for entry in items:
            if entry.temp_item_id in seen:
                raise FriendlyException(
                    error_codes.VALIDATION_ERROR,
                    f"Duplicate temp_item_id: {entry.temp_item_id}",
                    status_code=422,
                )
            seen.add(entry.temp_item_id)
            if entry.temp_item_id not in candidate_by_temp_id:
                raise FriendlyException(
                    error_codes.VALIDATION_ERROR,
                    f"Unknown temp_item_id: {entry.temp_item_id}",
                    status_code=422,
                )

        created: list[tuple[str, WardrobeItem]] = []
        for entry in items:
            item = await self._create_item(
                user_id=user_id,
                candidate=candidate_by_temp_id[entry.temp_item_id],
                fallback_image_url=task.input_payload.get("image_url"),
                title=entry.title,
                category=entry.category,
                color_main=entry.color_main,
                location=entry.location,
                size_label=entry.size_label,
                fit_status=entry.fit_status,
                fit_intent=entry.fit_intent,
            )
            created.append((entry.temp_item_id, item))

        task.task_status = ImportStatus.COMPLETED.value
        await self.import_task_repository.flush()
        await schedule_today_recompute(user_id, self.recommendation_repository, self.style_repository)
        return created

    async def _load_waiting_task(self, task_id: uuid.UUID, user_id: uuid.UUID) -> ImportTask:
        task = await self.import_task_repository.get_for_user(task_id, user_id, for_update=True)
        if task is None:
            raise FriendlyException(error_codes.NOT_FOUND, "Import task not found", status_code=404)
        if task.task_status != ImportStatus.WAITING_CONFIRM.value:
            raise FriendlyException(
                error_codes.STATE_CONFLICT,
                "Import task is not waiting for confirmation",
                status_code=409,
            )
        return task

    async def _create_item(
        self,
        *,
        user_id: uuid.UUID,
        candidate: dict[str, Any],
        fallback_image_url: Any,
        title: str,
        category: str,
        color_main: str,
        location: str | None = None,
        size_label: str | None = None,
        fit_status: str | None = None,
        fit_intent: str | None = None,
        purchase_price: Decimal | None = None,
        purchase_date: date | None = None,
    ) -> WardrobeItem:
        original_image_url = candidate.get("original_image_url")
        if original_image_url is None:
            original_image_url = fallback_image_url

        # BE#25 — the standard/cover image prefers a polished cutout, then a
        # tight crop, then the raw photo. Single confirm's candidate carries
        # neither field, so it falls back to original (behavior unchanged).
        cutout_image_url = candidate.get("cutout_image_url")
        crop_image_url = candidate.get("crop_image_url")
        standard_image_url = cutout_image_url or crop_image_url or original_image_url

        # M18 — auto-fill from the OCR candidate when the client didn't override.
        # candidate_payload stores price/date as strings (JSONB-safe), so coerce.
        if size_label is None:
            size_label = candidate.get("size_label")
        if purchase_price is None:
            purchase_price = _to_decimal(candidate.get("purchase_price"))
        if purchase_date is None:
            purchase_date = _to_date(candidate.get("purchase_date"))

        # BE#30 — carry the AI naming provenance onto the item. The user's
        # typed title always wins (it's what `title` holds). name_source flags
        # whether the title still matches the AI display name ('ai') or the
        # user changed it ('user') — provenance for any future re-namer.
        canonical_name = candidate.get("canonical_name")
        localized_names = candidate.get("localized_names")
        if not isinstance(localized_names, dict):
            localized_names = None
        # OCR/receipt candidates carry `title` but no `display_name`; that title
        # was still pipeline-generated, so fall back to it before deciding the
        # name was a user edit. Compare trimmed so whitespace alone doesn't flip
        # an otherwise-accepted AI name to 'user'.
        ai_display_name = candidate.get("display_name") or candidate.get("title")
        name_source = "ai" if title.strip() == str(ai_display_name or "").strip() else "user"

        item = WardrobeItem(
            user_id=user_id,
            title=title,
            canonical_name=canonical_name if isinstance(canonical_name, str) else None,
            localized_names=localized_names,
            name_source=name_source,
            category=category,
            color_main=color_main,
            status=ItemStatus.WEARABLE.value,
            cover_image_url=standard_image_url,
            original_image_url=original_image_url,
            location=location,
            size_label=size_label,
            fit_status=fit_status or "unknown",
            # BE#121 — the fit-intent chips the confirm form collects were
            # previously dropped on the floor; persist them (schema-validated).
            fit_intent=fit_intent,
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
                    image_url=standard_image_url,
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
