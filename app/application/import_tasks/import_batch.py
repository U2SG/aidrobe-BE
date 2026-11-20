"""Album scan read model: one batch of photo imports, grouped by garment."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import Any, Literal

import structlog
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.import_tasks.batch_groups import (
    BatchCandidate,
    GarmentImage,
    SameGarmentJudge,
    best_member,
    color_family,
    garment_kind,
    group_candidates,
    kinds_compatible,
)
from app.application.import_tasks.candidates import PARTIALLY_HIDDEN, confirmed_item_ids, normalize_candidates
from app.application.ports.vision import VisionPort
from app.common import error_codes
from app.common.enums import ImageType, ImportStatus, InputType
from app.common.exceptions import FriendlyException
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.import_task import ImportBatchVerdict, ImportTask
from app.infrastructure.persistence.models.wardrobe import WardrobeItem, WardrobeItemImage
from app.infrastructure.storage.object_key import is_legacy_garment_crop
from app.infrastructure.workers.garment_crops import CROP_JOB_TYPE
from app.infrastructure.workers.job_runtime import JOB_STATUS_QUEUED, JOB_STATUS_RUNNING

logger = structlog.get_logger(__name__)

#: New vision comparisons one read may spend; the rest wait for the next read (`complete=false`).
MAX_NEW_COMPARISONS = 32
COMPARISON_CONCURRENCY = 8
#: Wardrobe look-alikes checked per group, newest first.
EXISTING_MATCH_LIMIT = 3
#: Candidate ids the user skipped in a batch confirmation, on `input_payload`.
DISMISSED_KEY = "dismissed_items"

ImageLoader = Callable[[GarmentImage], Awaitable[bytes]]
GroupKind = Literal["new", "existing", "uncertain"]
GroupStatus = Literal["pending", "confirmed", "skipped"]


class CachedJudge:
    """Same-garment verdicts: identical image → same; cached → reuse; else ask vision within budget."""

    def __init__(
        self, vision: VisionPort, load: ImageLoader, cache: dict[tuple[str, str], bool],
        *, budget: int = MAX_NEW_COMPARISONS, concurrency: int = COMPARISON_CONCURRENCY,
    ) -> None:
        self.vision = vision
        self.load = load
        self.cache = cache
        self.budget = budget
        self.new: dict[tuple[str, str], bool] = {}
        self._semaphore = asyncio.Semaphore(concurrency)
        self._inflight: dict[tuple[str, str], asyncio.Task[bool | None]] = {}
        self._images: dict[str, asyncio.Task[bytes]] = {}

    async def __call__(self, a: GarmentImage, b: GarmentImage, garment: str) -> bool | None:
        if a.ref is None or b.ref is None:
            return None
        if a.ref == b.ref:
            return True
        ref_a, ref_b = sorted((a.ref, b.ref))
        pair = (ref_a, ref_b)
        if pair in self.cache:
            return self.cache[pair]
        if pair not in self._inflight:
            if self.budget <= 0:
                return None
            self.budget -= 1
            self._inflight[pair] = asyncio.create_task(self._compare(pair, a, b, garment))
        return await self._inflight[pair]

    async def _compare(self, pair: tuple[str, str], a: GarmentImage, b: GarmentImage, garment: str) -> bool | None:
        try:
            async with self._semaphore:
                crop_a, crop_b = await asyncio.gather(self._image(a), self._image(b))
                verdict = await self.vision.same_garment(crop_a, crop_b, garment)
        except Exception as exc:
            logger.warning("import_batch.same_garment_failed", error=type(exc).__name__)
            return None
        self.cache[pair] = self.new[pair] = verdict
        return verdict

    def _image(self, image: GarmentImage) -> asyncio.Task[bytes]:
        assert image.ref is not None
        if image.ref not in self._images:
            self._images[image.ref] = asyncio.ensure_future(self.load(image))
        return self._images[image.ref]


@dataclass(frozen=True)
class BatchGroup:
    group_id: uuid.UUID
    kind: GroupKind
    status: GroupStatus
    members: list[BatchCandidate]
    best: BatchCandidate
    existing_item: WardrobeItem | None


@dataclass(frozen=True)
class BatchView:
    batch_id: uuid.UUID
    tasks: list[ImportTask]
    total_photos: int
    processing: int
    failed: int
    groups: list[BatchGroup]
    complete: bool

    def group(self, group_id: uuid.UUID) -> BatchGroup | None:
        return next((group for group in self.groups if group.group_id == group_id), None)


async def batch_tasks(
    session: AsyncSession, user_id: uuid.UUID, batch_id: uuid.UUID, *, for_update: bool = False
) -> list[ImportTask]:
    stmt = (
        select(ImportTask)
        .where(ImportTask.user_id == user_id, ImportTask.batch_id == batch_id)
        .order_by(ImportTask.created_at, ImportTask.id)
    )
    if for_update:
        stmt = stmt.with_for_update().execution_options(populate_existing=True)
    return list(await session.scalars(stmt))


def dismissed_ids(task: ImportTask) -> set[str]:
    raw = (task.input_payload or {}).get(DISMISSED_KEY)
    return {str(value) for value in raw} if isinstance(raw, list) else set()


def importable_candidates(task: ImportTask) -> list[dict[str, Any]]:
    return [raw for raw in normalize_candidates(task) if raw.get("should_import") is not False]


def _candidate_image(task: ImportTask, raw: dict[str, Any]) -> GarmentImage | None:
    crop_key, crop_url = raw.get("crop_object_key"), raw.get("crop_image_url")
    if (crop_key or crop_url) and not is_legacy_garment_crop(crop_key or crop_url, task.user_id):
        return GarmentImage(key=crop_key, url=crop_url)
    if task.input_type == InputType.IMAGE.value and len(normalize_candidates(task)) == 1:
        # A flat-lay photo is the garment itself.
        payload = task.input_payload or {}
        key, url = payload.get("source_object_key"), payload.get("image_url")
        if key or url:
            return GarmentImage(key=key, url=url)
    return None


def _to_uuid(value: str | None) -> uuid.UUID | None:
    try:
        return uuid.UUID(value) if value else None
    except ValueError:
        return None


def batch_candidates(tasks: list[ImportTask]) -> list[BatchCandidate]:
    candidates: list[BatchCandidate] = []
    for task in tasks:
        if task.task_status not in (ImportStatus.WAITING_CONFIRM.value, ImportStatus.COMPLETED.value):
            continue
        confirmed = confirmed_item_ids(task)
        dismissed = dismissed_ids(task)
        for raw in importable_candidates(task):
            temp_id = raw["temp_item_id"]
            try:
                confidence = float(raw.get("confidence_score") or 0.0)
            except (TypeError, ValueError):
                confidence = 0.0
            candidates.append(
                BatchCandidate(
                    task_id=task.id,
                    temp_item_id=temp_id,
                    category=str(raw.get("category") or "other"),
                    color_main=str(raw.get("color_main") or ""),
                    title=str(raw.get("display_name") or raw.get("title") or ""),
                    canonical_name=raw.get("canonical_name"),
                    image=_candidate_image(task, raw),
                    confidence=confidence,
                    partially_hidden=raw.get("visibility") == PARTIALLY_HIDDEN,
                    taken_at=task.taken_at,
                    confirmed_item_id=_to_uuid(confirmed.get(temp_id)),
                    dismissed=temp_id in dismissed,
                )
            )
    return candidates


def item_kind(item: WardrobeItem) -> str | None:
    return garment_kind(item.canonical_name if item.name_source != "user" else None, item.title)


async def _item_images(session: AsyncSession, items: list[WardrobeItem]) -> dict[uuid.UUID, GarmentImage]:
    """The real-photo image of each item: its verified crop, else its cover, else the original photo."""
    if not items:
        return {}
    rows = await session.scalars(
        select(WardrobeItemImage)
        .where(WardrobeItemImage.item_id.in_([item.id for item in items]))
        .order_by(WardrobeItemImage.created_at.desc(), WardrobeItemImage.id)
    )
    preference = {ImageType.CROP.value: 0, ImageType.STANDARD.value: 1, ImageType.ORIGINAL.value: 2}
    chosen: dict[uuid.UUID, WardrobeItemImage] = {}
    for row in rows:
        rank = preference.get(row.image_type)
        if rank is None:
            continue
        current = chosen.get(row.item_id)
        if current is None or rank < preference[current.image_type]:
            chosen[row.item_id] = row
    return {item_id: GarmentImage(key=row.object_key, url=row.image_url) for item_id, row in chosen.items()}


async def load_batch_view(
    session: AsyncSession,
    user_id: uuid.UUID,
    batch_id: uuid.UUID,
    vision: VisionPort,
    load_image: ImageLoader,
    *,
    tasks: list[ImportTask] | None = None,
    release_connection: bool = False,
) -> BatchView:
    if tasks is None:
        tasks = await batch_tasks(session, user_id, batch_id)
    if not tasks:
        raise FriendlyException(error_codes.NOT_FOUND, "Import batch not found", status_code=404)
    task_ids = [task.id for task in tasks]
    cropping = set(
        await session.scalars(
            select(AsyncJob.target_id).where(
                AsyncJob.job_type == CROP_JOB_TYPE,
                AsyncJob.target_id.in_(task_ids),
                AsyncJob.status.in_((JOB_STATUS_QUEUED, JOB_STATUS_RUNNING)),
            )
        )
    )
    active = (ImportStatus.QUEUED.value, ImportStatus.PROCESSING.value)
    processing = sum(1 for task in tasks if task.task_status in active or task.id in cropping)
    failed = sum(1 for task in tasks if task.task_status == ImportStatus.FAILED.value)

    candidates = batch_candidates(tasks)
    categories = {candidate.category for candidate in candidates}
    confirmed_ids = {c.confirmed_item_id for c in candidates if c.confirmed_item_id is not None}
    items = list(
        await session.scalars(
            select(WardrobeItem)
            .where(
                WardrobeItem.user_id == user_id,
                WardrobeItem.deleted_at.is_(None),
                or_(WardrobeItem.category.in_(categories), WardrobeItem.id.in_(confirmed_ids)),
            )
            .order_by(WardrobeItem.created_at.desc(), WardrobeItem.id.desc())
        )
    ) if candidates else []
    items_by_id = {item.id: item for item in items}
    images = await _item_images(session, items)
    cache = {
        (row.image_a, row.image_b): row.same
        for row in await session.scalars(
            select(ImportBatchVerdict).where(
                ImportBatchVerdict.user_id == user_id, ImportBatchVerdict.batch_id == batch_id,
            )
        )
    }
    if release_connection:
        # Vision calls take seconds each; don't hold a pooled connection meanwhile.
        await session.commit()

    judge = CachedJudge(vision, load_image, cache)
    # Members confirmed to an item that was since deleted are back to undecided.
    live = [
        candidate if candidate.confirmed_item_id in items_by_id or candidate.confirmed_item_id is None
        else replace(candidate, confirmed_item_id=None)
        for candidate in candidates
    ]
    grouping = await group_candidates(live, judge)
    results = await asyncio.gather(
        *(_classify(batch_id, members, items, items_by_id, images, judge) for members in grouping.groups)
    )
    groups = [group for group, _ in results]
    pending = grouping.pending or any(group_pending for _, group_pending in results)

    if judge.new:
        await session.execute(
            insert(ImportBatchVerdict)
            .values([
                {"id": uuid.uuid4(), "user_id": user_id, "batch_id": batch_id, "image_a": a, "image_b": b, "same": same}
                for (a, b), same in judge.new.items()
            ])
            .on_conflict_do_nothing(index_elements=["user_id", "batch_id", "image_a", "image_b"])
        )
    return BatchView(
        batch_id=batch_id, tasks=tasks, total_photos=len(tasks), processing=processing, failed=failed,
        groups=groups, complete=processing == 0 and not pending,
    )


async def _classify(
    batch_id: uuid.UUID,
    members: list[BatchCandidate],
    items: list[WardrobeItem],
    items_by_id: dict[uuid.UUID, WardrobeItem],
    images: dict[uuid.UUID, GarmentImage],
    judge: SameGarmentJudge,
) -> tuple[BatchGroup, bool]:
    best = best_member(members)
    group_id = uuid.uuid5(batch_id, members[0].key)
    confirmed = next(
        (items_by_id[m.confirmed_item_id] for m in members if m.confirmed_item_id in items_by_id), None
    )
    if confirmed is not None:
        return BatchGroup(group_id, "existing", "confirmed", members, best, confirmed), False
    status: GroupStatus = "skipped" if all(m.dismissed for m in members) else "pending"
    lookalikes = [
        item for item in items
        if (item.category, color_family(item.color_main)) == best.bucket
        and kinds_compatible(item_kind(item), best.kind)
    ][:EXISTING_MATCH_LIMIT]
    if not best.mergeable or best.image is None:
        kind: GroupKind = "uncertain"
        return BatchGroup(group_id, kind, status, members, best, lookalikes[0] if lookalikes else None), False
    checked = [item for item in lookalikes if item.id in images]
    verdicts = await asyncio.gather(*(judge(best.image, images[item.id], best.label) for item in checked))
    match = next((item for item, verdict in zip(checked, verdicts, strict=True) if verdict), None)
    if match is not None:
        return BatchGroup(group_id, "existing", status, members, best, match), False
    pending = any(verdict is None for verdict in verdicts)
    if lookalikes:
        return BatchGroup(group_id, "uncertain", status, members, best, lookalikes[0]), pending
    return BatchGroup(group_id, "new", status, members, best, None), pending
