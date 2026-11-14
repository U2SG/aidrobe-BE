import uuid
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy import select

from app.application.import_tasks.candidates import confirmed_item_ids, normalize_candidates, store_candidates
from app.application.import_tasks.prepare_derivatives import prepare_item_derivatives
from app.application.import_tasks.source import source_version, upload_key
from app.application.recommendations.background import schedule_today_recompute
from app.common import error_codes
from app.common.enums import CutoutStatus, ImageType, ImportStatus, InputType, ItemStatus
from app.common.exceptions import FriendlyException
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
from app.infrastructure.storage.source_image import InvalidSourceImage


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
    purchase_price: Decimal | None = None
    purchase_date: date | None = None


@dataclass(frozen=True)
class ConfirmedEntry:
    temp_item_id: str
    item: WardrobeItem
    created: bool  # False = replay of an earlier confirmation


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
    ) -> ConfirmedEntry:
        task = await self._load_task(task_id, user_id)
        candidates = normalize_candidates(task)
        if not candidates:
            self._require_waiting(task)
            raise FriendlyException(error_codes.STATE_CONFLICT, "Import task has no candidate", 409)
        entry = ConfirmItemInput(
            candidates[0]["temp_item_id"], title, category, color_main,
            location, size_label, fit_status, fit_intent, purchase_price, purchase_date,
        )
        return (await self._confirm(task, [entry], dismiss_remaining=False))[0]

    async def confirm_items(
        self,
        task_id: uuid.UUID,
        user_id: uuid.UUID,
        items: list[ConfirmItemInput],
        *,
        dismiss_remaining: bool = False,
    ) -> list[ConfirmedEntry]:
        if not items and not dismiss_remaining:
            raise FriendlyException(
                error_codes.VALIDATION_ERROR, "Select items or dismiss the remaining candidates", 422
            )
        task = await self._load_task(task_id, user_id)
        return await self._confirm(task, items, dismiss_remaining=dismiss_remaining)

    async def _confirm(
        self, task: ImportTask, items: list[ConfirmItemInput], *, dismiss_remaining: bool
    ) -> list[ConfirmedEntry]:
        confirmed = confirmed_item_ids(task)
        candidates = normalize_candidates(task)
        by_id = {candidate["temp_item_id"]: candidate for candidate in candidates}
        if task.task_status != ImportStatus.WAITING_CONFIRM.value:
            replay = task.task_status == ImportStatus.COMPLETED.value and all(
                entry.temp_item_id in confirmed for entry in items
            )
            if not replay:
                self._require_waiting(task)

        seen: set[str] = set()
        results: list[ConfirmedEntry] = []
        for entry in items:
            candidate_id = entry.temp_item_id
            if candidate_id in seen or candidate_id not in by_id:
                raise FriendlyException(error_codes.VALIDATION_ERROR, "Invalid or duplicate candidate ID", 422)
            seen.add(candidate_id)
            if candidate_id in confirmed:
                existing = await self._existing_item(confirmed[candidate_id], task.user_id)
                if existing is None:
                    raise FriendlyException(error_codes.STATE_CONFLICT, "Confirmed item was removed", 409)
                results.append(ConfirmedEntry(candidate_id, existing, created=False))
                continue
            item = await self._create_item(task, by_id[candidate_id], entry)
            confirmed[candidate_id] = str(item.id)
            results.append(ConfirmedEntry(candidate_id, item, created=True))

        if task.task_status == ImportStatus.WAITING_CONFIRM.value:
            if candidates:
                store_candidates(task, candidates)
            task.input_payload = {**task.input_payload, "confirmed_items": confirmed}
            if dismiss_remaining or not (by_id.keys() - confirmed.keys()):
                task.task_status = ImportStatus.COMPLETED.value
            await self.import_task_repository.flush()
        if any(entry.created for entry in results):
            await schedule_today_recompute(task.user_id, self.recommendation_repository, self.style_repository)
        return results

    async def _load_task(self, task_id: uuid.UUID, user_id: uuid.UUID) -> ImportTask:
        # FOR UPDATE: a concurrent confirm waits, then sees the recorded
        # confirmation and replays it instead of creating a duplicate item.
        task = await self.import_task_repository.get_for_user(task_id, user_id, for_update=True)
        if task is None:
            raise FriendlyException(error_codes.NOT_FOUND, "Import task not found", status_code=404)
        return task

    @staticmethod
    def _require_waiting(task: ImportTask) -> None:
        if task.task_status != ImportStatus.WAITING_CONFIRM.value:
            raise FriendlyException(
                error_codes.STATE_CONFLICT,
                "Import task is not waiting for confirmation",
                status_code=409,
            )

    async def _existing_item(self, item_id: str, user_id: uuid.UUID) -> WardrobeItem | None:
        try:
            parsed = uuid.UUID(item_id)
        except ValueError:
            return None
        return await self.wardrobe_repository.get_for_user(parsed, user_id)

    async def _create_item(
        self, task: ImportTask, candidate: dict[str, Any], entry: ConfirmItemInput
    ) -> WardrobeItem:
        original_url = candidate.get("original_image_url") or task.input_payload.get("image_url")
        original_key = task.input_payload.get("source_object_key")
        if not original_key:
            try:
                original_key = upload_key(original_url, task.user_id)
            except InvalidSourceImage:
                raise FriendlyException(error_codes.VALIDATION_ERROR, "Invalid import image", 422) from None
        crop_url = candidate.get("crop_image_url")
        crop_key = candidate.get("crop_object_key")
        generated_url = candidate.get("cutout_image_url")
        cover_url = generated_url or crop_url or original_url
        cover_key = None if generated_url else (crop_key if crop_url else original_key)
        ai_title = candidate.get("display_name") or candidate.get("title")
        uses_ai_name = (
            entry.title.strip() == str(ai_title or "").strip() and entry.category == candidate.get("category")
        )
        canonical_name = candidate.get("canonical_name")
        localized_names = candidate.get("localized_names")

        item = await self.wardrobe_repository.add(
            WardrobeItem(
                user_id=task.user_id,
                title=entry.title,
                canonical_name=canonical_name if isinstance(canonical_name, str) else None,
                localized_names=localized_names if isinstance(localized_names, dict) else None,
                name_source="ai" if uses_ai_name else "user",
                category=entry.category,
                color_main=entry.color_main,
                status=ItemStatus.WEARABLE.value,
                cover_image_url=cover_url,
                original_image_url=original_url,
                location=entry.location,
                size_label=entry.size_label if entry.size_label is not None else candidate.get("size_label"),
                fit_status=entry.fit_status or "unknown",
                fit_intent=entry.fit_intent,
                purchase_price=(
                    entry.purchase_price if entry.purchase_price is not None
                    else _to_decimal(candidate.get("purchase_price"))
                ),
                purchase_date=entry.purchase_date or _to_date(candidate.get("purchase_date")),
                **_recognised_attributes(candidate, confirmed_category=entry.category),
            )
        )
        for image_type, url, key in (
            (ImageType.ORIGINAL, original_url, original_key),
            (ImageType.STANDARD, cover_url, cover_key),
            (ImageType.CROP, crop_url, crop_key),
        ):
            if url:
                await self.wardrobe_repository.add_image(
                    WardrobeItemImage(
                        item_id=item.id, image_type=image_type.value, image_url=url,
                        object_key=key, created_at=datetime.now(UTC),
                    )
                )
        shared_photo = task.input_type == InputType.OUTFIT_IMAGE or (
            len(normalize_candidates(task)) > 1 and original_url == task.input_payload.get("image_url")
        )
        real_url, real_key = (crop_url, crop_key) if shared_photo or crop_url else (original_url, original_key)
        # Receipt screenshots are purchase evidence, not garment photos.
        if (real_key or real_url) and task.input_type not in _RECEIPT_TYPES:
            await prepare_item_derivatives(
                self.wardrobe_repository.session, item,
                source_key=real_key, source_url=real_url, source_version=source_version(real_key, real_url),
            )
        return item


_RECEIPT_TYPES = {InputType.RECEIPT_SCREENSHOT, InputType.ORDER_SCREENSHOT, InputType.SHOPPING_EMAIL}


async def apply_confirmed_crop(
    repository: SqlAlchemyWardrobeRepository,
    *,
    user_id: uuid.UUID,
    item_id: uuid.UUID,
    original_url: str,
    crop_url: str,
    crop_key: str,
) -> None:
    session = repository.session
    item = await session.scalar(
        select(WardrobeItem)
        .where(
            WardrobeItem.id == item_id, WardrobeItem.user_id == user_id, WardrobeItem.deleted_at.is_(None),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if item is None or item.original_image_url != original_url or item.embedding_source_version is not None:
        return
    await repository.add_image(
        WardrobeItemImage(
            item_id=item.id, image_type=ImageType.CROP.value,
            image_url=crop_url, object_key=crop_key, created_at=datetime.now(UTC),
        )
    )
    if item.cutout_status != CutoutStatus.READY.value and item.cover_image_url == original_url:
        item.cover_image_url = crop_url
        standard = await session.scalar(
            select(WardrobeItemImage).where(
                WardrobeItemImage.item_id == item.id, WardrobeItemImage.image_type == ImageType.STANDARD.value,
            )
        )
        if standard is not None:
            standard.image_url = crop_url
            standard.object_key = crop_key
    await prepare_item_derivatives(
        session, item, source_key=crop_key, source_url=crop_url, source_version=source_version(crop_key, crop_url),
    )


def _recognised_attributes(candidate: dict[str, Any], *, confirmed_category: str) -> dict[str, Any]:
    """Recognised attributes from the candidate, in WardrobeItem's shapes.

    Unknown stays NULL (never a default), so rankers can tell "not recognised"
    from "recognised as neutral". season_tags uses the `{"tags": [...]}` shape
    the season readers (obstacles, scoring, wardrobe filter) expect.

    When the user corrected the category, recognition looked at (or imagined)
    a different garment, so its attributes are dropped rather than trusted.
    """
    recognised_category = candidate.get("category")
    if isinstance(recognised_category, str) and recognised_category != confirmed_category:
        return {}
    attributes: dict[str, Any] = {}
    formality = candidate.get("formality_score")
    if isinstance(formality, int) and not isinstance(formality, bool) and 0 <= formality <= 100:
        attributes["formality_score"] = formality
    seasons = candidate.get("season_tags")
    if isinstance(seasons, list) and seasons:
        attributes["season_tags"] = {"tags": [str(s) for s in seasons]}
    material = candidate.get("material")
    if isinstance(material, str) and material:
        attributes["material"] = material[:64]
    styles = candidate.get("style_tags")
    if isinstance(styles, list) and styles:
        attributes["style_tags"] = [str(t) for t in styles]
    return attributes


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
