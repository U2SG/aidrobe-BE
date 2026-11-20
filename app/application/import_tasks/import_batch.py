"""Album scan read model: one batch of photo imports, grouped by garment.

Reads never call the vision model. They group with the verdicts cached on the
batch and, while comparisons are missing, schedule one `garment_match` job
(job_runtime) that makes them — pre-screened locally, metered against the
user's daily quota — and caches the results for the next read.
"""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any, Literal

import structlog
from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.import_tasks.batch_groups import (
    STRICT_CATEGORIES,
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
from app.infrastructure.workers.job_runtime import (
    JOB_STATUS_DEAD,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    enqueue,
)

logger = structlog.get_logger(__name__)

MATCH_JOB_TYPE = "garment_match"
#: Vision calls one match job may make; a larger batch continues in a follow-up job.
MAX_CALLS_PER_RUN = 120
COMPARISON_CONCURRENCY = 8
#: Wardrobe look-alikes checked per group, newest first.
EXISTING_MATCH_LIMIT = 3
#: Candidate ids the user skipped in a batch confirmation, on `input_payload`.
DISMISSED_KEY = "dismissed_items"

ImageLoader = Callable[[GarmentImage], Awaitable[bytes]]
#: Local pre-screen: (crop_a, crop_b, same_photo) -> verdict, or None to ask the model.
Screen = Callable[[bytes, bytes, bool], bool | None]
GroupKind = Literal["new", "existing", "uncertain"]
GroupStatus = Literal["pending", "confirmed", "skipped"]
Pair = tuple[str, str]


class CachedJudge:
    """Same-garment verdicts: identical image → same; cached → reuse; else (with a
    vision port) pre-screen locally, then ask the model within `budget` calls.

    Without a vision port it only reads the cache and records what is missing.
    """

    def __init__(
        self,
        cache: dict[Pair, bool],
        vision: VisionPort | None = None,
        load: ImageLoader | None = None,
        *,
        budget: int = 0,
        concurrency: int = COMPARISON_CONCURRENCY,
        screen: Screen | None = None,
    ) -> None:
        self.cache = cache
        self.vision = vision
        self.load = load
        self.budget = budget
        self.screen = screen
        self.new: dict[Pair, bool] = {}
        self.missing: set[Pair] = set()
        self.calls = 0
        self.screened = 0
        self.errors = 0
        self.exhausted = False
        self._semaphore = asyncio.Semaphore(concurrency)
        self._inflight: dict[Pair, asyncio.Task[bool | None]] = {}
        self._images: dict[str, asyncio.Task[bytes]] = {}

    async def __call__(
        self, a: GarmentImage, b: GarmentImage, garment: str, *, strict: bool = False, same_photo: bool | None = None,
    ) -> bool | None:
        if a.ref is None or b.ref is None:
            return None
        if a.ref == b.ref:
            return True
        ref_a, ref_b = sorted((a.ref, b.ref))
        pair = (ref_a, ref_b)
        if pair in self.cache:
            return self.cache[pair]
        if self.vision is None or self.load is None:
            self.missing.add(pair)
            return None
        if pair not in self._inflight:
            self._inflight[pair] = asyncio.create_task(self._compare(pair, a, b, garment, strict, same_photo))
        return await self._inflight[pair]

    async def _compare(
        self, pair: Pair, a: GarmentImage, b: GarmentImage, garment: str, strict: bool, same_photo: bool | None,
    ) -> bool | None:
        assert self.vision is not None
        try:
            async with self._semaphore:
                crop_a, crop_b = await asyncio.gather(self._image(a), self._image(b))
                local = self.screen(crop_a, crop_b, same_photo) if self.screen and same_photo is not None else None
                if local is not None:
                    self.screened += 1
                    verdict = local
                else:
                    needed = 2 if strict else 1
                    if self.budget < needed:
                        self.exhausted = True
                        self.missing.add(pair)
                        return None
                    self.budget -= needed
                    self.calls += 1
                    verdict = await self.vision.same_garment(crop_a, crop_b, garment)
                    if strict:
                        if verdict:
                            # A look-alike category needs a second, independent yes
                            # (images swapped) before two photos merge.
                            self.calls += 1
                            verdict = await self.vision.same_garment(crop_b, crop_a, garment)
                        else:
                            self.budget += 1
        except Exception as exc:
            logger.warning("import_batch.same_garment_failed", error=type(exc).__name__)
            self.errors += 1
            self.missing.add(pair)
            return None
        self.cache[pair] = self.new[pair] = verdict
        return verdict

    def _image(self, image: GarmentImage) -> asyncio.Task[bytes]:
        assert image.ref is not None and self.load is not None
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
class BatchState:
    """Everything a grouping pass needs, read in one go."""

    user_id: uuid.UUID
    batch_id: uuid.UUID
    tasks: list[ImportTask]
    processing: int
    failed: int
    candidates: list[BatchCandidate]
    items: list[WardrobeItem]
    images: dict[uuid.UUID, GarmentImage]
    cache: dict[Pair, bool]

    @property
    def items_by_id(self) -> dict[uuid.UUID, WardrobeItem]:
        return {item.id: item for item in self.items}


@dataclass(frozen=True)
class BatchView:
    batch_id: uuid.UUID
    tasks: list[ImportTask]
    total_photos: int
    processing: int
    failed: int
    groups: list[BatchGroup]
    complete: bool
    # Comparisons stopped (daily quota, provider failure); the rest stay separate groups.
    comparison_limited: bool

    def group(self, group_id: uuid.UUID) -> BatchGroup | None:
        """The group with this id, or the group that has since absorbed it.

        A group's id derives from its first member, so a group merged into
        another (a later comparison, or two groups confirmed to one item) is
        still found through that member — a retried confirmation then replays
        instead of failing on an id the client saw a moment ago.
        """
        exact = next((group for group in self.groups if group.group_id == group_id), None)
        return exact or next(
            (
                group for group in self.groups
                if any(group_id_for(self.batch_id, member) == group_id for member in group.members)
            ),
            None,
        )


def group_id_for(batch_id: uuid.UUID, founder: BatchCandidate) -> uuid.UUID:
    return uuid.uuid5(batch_id, founder.key)


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
    """The verified crop. A crop the review rejected was never stored, so such a
    candidate has none and stays out of visual merging (its own uncertain group)."""
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
        fingerprint = (task.input_payload or {}).get("photo_fingerprint")
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
                    photo_fingerprint=fingerprint if isinstance(fingerprint, str) else None,
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


async def load_batch_state(
    session: AsyncSession, user_id: uuid.UUID, batch_id: uuid.UUID, *, tasks: list[ImportTask] | None = None,
) -> BatchState:
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
    live_ids = {item.id for item in items}
    # Members confirmed to an item that was since deleted are back to undecided.
    candidates = [
        c if c.confirmed_item_id is None or c.confirmed_item_id in live_ids else replace(c, confirmed_item_id=None)
        for c in candidates
    ]
    cache = {
        (row.image_a, row.image_b): row.same
        for row in await session.scalars(
            select(ImportBatchVerdict).where(
                ImportBatchVerdict.user_id == user_id, ImportBatchVerdict.batch_id == batch_id,
            )
        )
    }
    return BatchState(
        user_id=user_id, batch_id=batch_id, tasks=tasks,
        processing=sum(1 for task in tasks if task.task_status in active or task.id in cropping),
        failed=sum(1 for task in tasks if task.task_status == ImportStatus.FAILED.value),
        candidates=candidates, items=items, images=await _item_images(session, items), cache=cache,
    )


async def group_batch(state: BatchState, judge: SameGarmentJudge) -> tuple[list[BatchGroup], bool]:
    """Groups and whether any comparison is still unanswered."""
    grouping = await group_candidates(state.candidates, judge)
    items_by_id = state.items_by_id
    results = await asyncio.gather(
        *(_classify(state, members, items_by_id, judge) for members in grouping.groups)
    )
    return [group for group, _ in results], grouping.pending or any(pending for _, pending in results)


async def _classify(
    state: BatchState, members: list[BatchCandidate], items_by_id: dict[uuid.UUID, WardrobeItem],
    judge: SameGarmentJudge,
) -> tuple[BatchGroup, bool]:
    best = best_member(members)
    group_id = group_id_for(state.batch_id, members[0])
    confirmed = next(
        (items_by_id[m.confirmed_item_id] for m in members if m.confirmed_item_id in items_by_id), None
    )
    if confirmed is not None:
        return BatchGroup(group_id, "existing", "confirmed", members, best, confirmed), False
    status: GroupStatus = "skipped" if all(m.dismissed for m in members) else "pending"
    lookalikes = [
        item for item in state.items
        if (item.category, color_family(item.color_main)) == best.bucket
        and kinds_compatible(item_kind(item), best.kind)
    ][:EXISTING_MATCH_LIMIT]
    # Shown to the user as "is it your …?" without a model yes: only when both garment kinds are
    # known and equal (an unknown kind must not pair a necklace with a cap).
    suggestion = next(
        (item for item in lookalikes if best.kind is not None and item_kind(item) == best.kind), None
    )
    if not best.mergeable or best.image is None:
        return BatchGroup(group_id, "uncertain", status, members, best, suggestion), False
    checked = [item for item in lookalikes if item.id in state.images]
    verdicts = await asyncio.gather(
        *(
            # A wardrobe image may be a product cutout, so no local pre-screen (same_photo=None).
            judge(best.image, state.images[item.id], best.label, strict=best.category in STRICT_CATEGORIES)
            for item in checked
        )
    )
    match = next((item for item, verdict in zip(checked, verdicts, strict=True) if verdict), None)
    if match is not None:
        return BatchGroup(group_id, "existing", status, members, best, match), False
    pending = any(verdict is None for verdict in verdicts)
    if suggestion is not None or (pending and checked):
        # An unanswered comparison hasn't ruled out an existing item: stay uncertain.
        return BatchGroup(group_id, "uncertain", status, members, best, suggestion), pending
    return BatchGroup(group_id, "new", status, members, best, None), pending


def match_job_key(batch_id: uuid.UUID, missing: set[Pair]) -> str:
    """One job per batch, per set of open comparisons, per day: a finished job is
    not re-run for the same open pairs until tomorrow's quota."""
    digest = hashlib.sha256("\n".join(f"{a}\t{b}" for a, b in sorted(missing)).encode()).hexdigest()[:32]
    return f"{MATCH_JOB_TYPE}:{batch_id}:{datetime.now(UTC).date().isoformat()}:{digest}"


async def schedule_match_job(session: AsyncSession, state: BatchState, missing: set[Pair]) -> bool:
    """Make sure a job will answer `missing`. Returns True when none will today
    (the job for exactly these pairs already ran and stopped: quota / provider)."""
    key = match_job_key(state.batch_id, missing)
    done = await session.scalar(select(AsyncJob.status).where(AsyncJob.idempotency_key == key))
    if done is not None:
        return done in (JOB_STATUS_SUCCEEDED, JOB_STATUS_DEAD)
    active = await session.scalar(
        select(AsyncJob.id).where(
            AsyncJob.job_type == MATCH_JOB_TYPE,
            AsyncJob.target_id == state.batch_id,
            AsyncJob.user_id == state.user_id,
            AsyncJob.status.in_((JOB_STATUS_QUEUED, JOB_STATUS_RUNNING)),
        ).limit(1)
    )
    if active is None:
        await enqueue(
            session, job_type=MATCH_JOB_TYPE, idempotency_key=key, user_id=state.user_id,
            target_id=state.batch_id, payload={"batch_id": str(state.batch_id)},
        )
    return False


async def read_batch_view(
    session: AsyncSession, user_id: uuid.UUID, batch_id: uuid.UUID, *,
    tasks: list[ImportTask] | None = None, schedule: bool = True,
) -> BatchView:
    """The batch as grouped by cached verdicts only; schedules the missing comparisons."""
    state = await load_batch_state(session, user_id, batch_id, tasks=tasks)
    judge = CachedJudge(state.cache)
    groups, pending = await group_batch(state, judge)
    limited = False
    if pending and schedule:
        limited = await schedule_match_job(session, state, judge.missing)
    return BatchView(
        batch_id=batch_id, tasks=state.tasks, total_photos=len(state.tasks), processing=state.processing,
        failed=state.failed, groups=groups,
        complete=state.processing == 0 and (not pending or limited), comparison_limited=limited,
    )


async def save_verdicts(
    session: AsyncSession, user_id: uuid.UUID, batch_id: uuid.UUID, verdicts: dict[Pair, bool]
) -> None:
    if not verdicts:
        return
    await session.execute(
        insert(ImportBatchVerdict)
        .values([
            {"id": uuid.uuid4(), "user_id": user_id, "batch_id": batch_id, "image_a": a, "image_b": b, "same": same}
            for (a, b), same in verdicts.items()
        ])
        .on_conflict_do_nothing(index_elements=["user_id", "batch_id", "image_a", "image_b"])
    )
