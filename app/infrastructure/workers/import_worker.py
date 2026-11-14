import asyncio
import uuid
from typing import Any, cast

import structlog
from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports.link_parser import LinkParserPort
from app.application.ports.ocr import OcrPort
from app.application.ports.rate_limit import UserRateLimiter
from app.application.ports.vision import VisionPort, VisionResult
from app.bootstrap.container import (
    link_parser,
    ocr_service,
    vision_rate_limiter,
    vision_service,
)
from app.common.enums import ImportStatus, InputType
from app.common.locale import resolve_locale
from app.infrastructure.importers.ocr_router import OcrCandidateAdapter, ocr_adapter_for
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.import_task import ImportTask
from app.infrastructure.persistence.models.user import UserSettings
from app.infrastructure.persistence.repositories.import_task_repo import (
    SqlAlchemyImportTaskRepository,
)
from app.infrastructure.persistence.session import AsyncSessionLocal
from app.infrastructure.workers.garment_crops import attach_garment_crops
from app.infrastructure.workers.job_runtime import (
    JOB_STATUS_DEAD,
    claim_one,
    enqueue,
    mark_failed,
    mark_succeeded,
    recover_expired_leases,
)

logger = structlog.get_logger(__name__)

# BE#26 — import processing on `job_runtime` (lease / retry / dead-letter)
# instead of FastAPI BackgroundTasks. Gated by ENABLE_IMPORT_ASYNC_JOBS; the
# job status only tracks EXECUTION, the `import_tasks.task_status` stays the
# business source of truth.
IMPORT_JOB_TYPE = "import_task"
# Vision/OCR is ~seconds and the user is polling, so drain a few per tick.
IMPORT_MAX_DRAIN_PER_TICK = 10
IMPORT_ATTEMPT_TIMEOUT_SECONDS = 60
# Leave time to roll back and finalize after a timed-out attempt.
IMPORT_LEASE_SECONDS = IMPORT_ATTEMPT_TIMEOUT_SECONDS + 15


def _import_idempotency_key(task_id: uuid.UUID) -> str:
    return f"import_task:{task_id}"


def _is_unverified_layer(candidate: dict[str, object], result: VisionResult) -> bool:
    if result.style_unverified:
        return True
    if candidate.get("crop_image_url"):
        return False
    name = f"{result.title} {result.display_name} {result.canonical_name}".casefold()
    return any(word in name for word in ("内搭", "打底", "inner layer", "base layer", "undershirt"))


class ProcessImageImportTask:
    def __init__(
        self,
        repository: SqlAlchemyImportTaskRepository,
        vision: VisionPort,
        parser: LinkParserPort | None = None,
        vision_rate_limiter: UserRateLimiter | None = None,
        ocr: OcrPort | None = None,
    ) -> None:
        self.repository = repository
        self.vision = vision
        self.parser = parser
        self.vision_rate_limiter = vision_rate_limiter
        self.ocr = ocr

    async def execute(self, task_id: uuid.UUID, user_id: uuid.UUID) -> None:
        task = await self.repository.get_for_user(task_id, user_id)
        if task is None:
            logger.warning("import_task_not_found_for_worker", task_id=str(task_id), user_id=str(user_id))
            return
        # Reprocess QUEUED (first run) AND PROCESSING (a prior async-job attempt
        # crashed mid-flight and is being retried — claim_one guarantees we're
        # the only worker on it now). A terminal task (WAITING_CONFIRM / FAILED)
        # no-ops so a lost finalizer's retry doesn't redo the vision call. The
        # BackgroundTasks path always enters at QUEUED and never re-enters.
        if task.task_status not in (ImportStatus.QUEUED.value, ImportStatus.PROCESSING.value):
            return

        task.task_status = ImportStatus.PROCESSING.value
        task.error_code = None
        task.error_message = None
        await self.repository.flush()

        # M18 — receipt / order-screenshot / shopping-email imports go through
        # the OCR pipeline instead of vision: the screenshot itself is OCR'd for
        # structured fields (title/category/color/size/price/date).
        ocr_adapter = ocr_adapter_for(task.input_type)
        if ocr_adapter is not None:
            await self._process_ocr(task, task_id, user_id, ocr_adapter)
            return

        try:
            image_url = task.input_payload.get("image_url")
            source_url = task.input_payload.get("source_url")
            if image_url is None and source_url is not None and self.parser is not None:
                image_url = await self.parser.extract_image_url(source_url)

            if not isinstance(image_url, str) or not image_url:
                raise ValueError("image_url is required")

            if self.vision_rate_limiter is not None:
                self.vision_rate_limiter.check(user_id)
            # BE#30 — name the candidate in the request locale, falling back to
            # the user's persisted settings locale (then the zh-CN default).
            locale = await self._resolve_locale(task, user_id)
            if task.input_type == InputType.OUTFIT_IMAGE.value:
                results = await self.vision.analyze_outfit(image_url, locale)
                if not results:
                    raise ValueError("No garments detected in outfit image")
            else:
                results = [await self.vision.analyze_image(image_url, locale)]
        except Exception as exc:
            logger.warning("import_task_processing_failed", task_id=str(task_id), error=str(exc))
            task.task_status = ImportStatus.FAILED.value
            task.error_code = "vision_failed"
            task.error_message = str(exc)
            await self.repository.flush()
            return

        task.task_status = ImportStatus.WAITING_CONFIRM.value
        candidate_items: list[dict[str, object]] = []
        for result in results:
            display_name = result.display_name or result.title
            candidate_items.append(
                {
                    "title": display_name,
                    "canonical_name": result.canonical_name or result.title,
                    "display_name": display_name,
                    "localized_names": result.localized_names,
                    "category": result.category,
                    "color_main": result.color_main,
                    "confidence_score": result.confidence_score,
                    "formality_score": result.formality_score,
                    "season_tags": list(result.season_tags),
                    "material": result.material,
                    "style_tags": list(result.style_tags),
                    "original_image_url": image_url,
                    "temp_item_id": uuid.uuid4().hex,
                    "should_import": True,
                }
            )

        if task.input_type == InputType.OUTFIT_IMAGE.value:
            # One picture per garment instead of the whole outfit photo.
            fingerprint = await attach_garment_crops(
                candidate_items, results, image_url=image_url, user_id=user_id, task_id=task_id, vision=self.vision
            )
            if fingerprint:
                task.input_payload = {**task.input_payload, "photo_fingerprint": fingerprint}
            candidate_items = [
                candidate
                for candidate, result in zip(candidate_items, results, strict=True)
                if not _is_unverified_layer(candidate, result)
            ]
            if not candidate_items:
                task.task_status = ImportStatus.FAILED.value
                task.error_code = "vision_failed"
                task.error_message = "No separately identifiable garments are visible"
                await self.repository.flush()
                return

        # Keep the first result in the legacy top-level shape while exposing all
        # garments through the existing multi-candidate contract.
        first_candidate = candidate_items[0]
        task.candidate_payload = {
            **{key: value for key, value in first_candidate.items() if key not in {"temp_item_id", "should_import"}},
            "candidate_items": candidate_items,
        }
        await self.repository.flush()

    async def _resolve_locale(self, task: ImportTask, user_id: uuid.UUID) -> str:
        request_locale = task.input_payload.get("locale")
        request_locale = request_locale if isinstance(request_locale, str) else None
        settings_locale = await self.repository.session.scalar(
            select(UserSettings.locale).where(UserSettings.user_id == user_id)
        )
        return resolve_locale(request_locale, settings_locale)

    async def _process_ocr(
        self,
        task: ImportTask,
        task_id: uuid.UUID,
        user_id: uuid.UUID,
        adapter: OcrCandidateAdapter,
    ) -> None:
        try:
            if self.ocr is None:
                raise ValueError("OCR provider is not configured")
            image_url = task.input_payload.get("image_url")
            if not isinstance(image_url, str) or not image_url:
                raise ValueError("image_url is required for OCR import")

            if self.vision_rate_limiter is not None:
                self.vision_rate_limiter.check(user_id)
            ocr_result = await self.ocr.extract_text(image_url)
        except Exception as exc:
            logger.warning("import_task_ocr_failed", task_id=str(task_id), error=str(exc))
            task.task_status = ImportStatus.FAILED.value
            task.error_code = "ocr_failed"
            task.error_message = str(exc)
            await self.repository.flush()
            return

        task.task_status = ImportStatus.WAITING_CONFIRM.value
        task.candidate_payload = adapter.to_candidate_payload(ocr_result, image_url)
        await self.repository.flush()


async def process_import_task_with_session(
    session: AsyncSession,
    task_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    worker = ProcessImageImportTask(
        SqlAlchemyImportTaskRepository(session),
        vision_service(),
        link_parser(),
        vision_rate_limiter(),
        ocr_service(),
    )
    await worker.execute(task_id, user_id)


async def process_import_task(ctx: object, task_id: str, user_id: str) -> None:
    """arq-compatible entry point for deployed workers."""
    async with AsyncSessionLocal() as session:
        await process_import_task_with_session(session, uuid.UUID(task_id), uuid.UUID(user_id))
        await session.commit()


async def enqueue_import_task(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    user_id: uuid.UUID,
    correlation_id: str | None = None,
) -> tuple[AsyncJob, bool]:
    """Idempotent (per-task) enqueue for import processing. Caller owns commit
    and must have already inserted the `import_tasks` row (status QUEUED)."""
    return await enqueue(
        session,
        job_type=IMPORT_JOB_TYPE,
        idempotency_key=_import_idempotency_key(task_id),
        payload={"task_id": str(task_id), "user_id": str(user_id)},
        user_id=user_id,
        target_id=task_id,
        correlation_id=correlation_id,
    )


async def _process_one_import(job: AsyncJob) -> None:
    """Run one claimed import job in its OWN session (never the request's), then
    finalize. Vision/OCR errors are caught inside `execute` and recorded as the
    task's business FAILED status, so the job still SUCCEEDS — only an
    unexpected crash fails the job (→ retry/backoff, then dead-letter)."""
    payload: dict[str, Any] = job.payload or {}
    task_id_str = payload.get("task_id")
    user_id_str = payload.get("user_id")
    if not task_id_str or not user_id_str:
        async with AsyncSessionLocal() as session:
            await mark_failed(session, job.id, claimed_attempt=job.attempts, error="missing payload fields")
            await session.commit()
        return

    task_id = uuid.UUID(task_id_str)
    user_id = uuid.UUID(user_id_str)
    structlog.contextvars.bind_contextvars(
        correlation_id=job.correlation_id or "",
        job_id=str(job.id),
        task_id=task_id_str,
    )
    try:
        # Fence the task mutation on this attempt's claim, in ONE transaction
        # (mirrors embedding/cutout): if a slow vision/OCR call outlived the
        # lease and the job was recovered + reclaimed elsewhere, `mark_succeeded`
        # returns False and we roll back the task write too — so a stale worker
        # never lands a status/candidate that could clobber the new claim's.
        async with AsyncSessionLocal() as session:
            async with asyncio.timeout(IMPORT_ATTEMPT_TIMEOUT_SECONDS):
                await process_import_task_with_session(session, task_id, user_id)
            ok = await mark_succeeded(session, job.id, claimed_attempt=job.attempts)
            if not ok:
                await session.rollback()
                logger.warning("import_task.lease_lost_discarding", job_id=str(job.id))
                return
            await session.commit()
    except Exception as exc:
        error = "import_attempt_timed_out" if isinstance(exc, TimeoutError) else str(exc)
        logger.warning("import_task.job_failed", task_id=task_id_str, error=error)
        async with AsyncSessionLocal() as session:
            status = await mark_failed(session, job.id, claimed_attempt=job.attempts, error=error)
            await session.commit()
        if status == JOB_STATUS_DEAD:
            await reconcile_dead_import_tasks(ctx=None)
    finally:
        structlog.contextvars.unbind_contextvars("correlation_id", "job_id", "task_id")


async def drain_import_jobs(ctx: object) -> int:
    """Recover interrupted imports and drain the next batch."""
    async with AsyncSessionLocal() as session:
        recovered = await recover_expired_leases(session, job_type=IMPORT_JOB_TYPE)
        await session.commit()
    if recovered:
        await reconcile_dead_import_tasks(ctx=None)

    processed = 0
    for _ in range(IMPORT_MAX_DRAIN_PER_TICK):
        async with AsyncSessionLocal() as session:
            job = await claim_one(session, [IMPORT_JOB_TYPE], lease_seconds=IMPORT_LEASE_SECONDS)
        if job is None:
            break
        await _process_one_import(job)
        processed += 1
    if processed:
        logger.info("import_task.drain_completed", processed=processed)
    return processed


async def reconcile_dead_import_tasks(ctx: object) -> int:
    """Flip tasks to FAILED when their import job dead-lettered (retries
    exhausted), so a crash-looped task doesn't sit in QUEUED/PROCESSING forever
    while the client polls it indefinitely."""
    async with AsyncSessionLocal() as session:
        dead_task_ids = (
            (
                await session.execute(
                    select(AsyncJob.target_id).where(
                        AsyncJob.job_type == IMPORT_JOB_TYPE,
                        AsyncJob.status == "dead",
                        AsyncJob.target_id.is_not(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        if not dead_task_ids:
            return 0
        res = cast(
            CursorResult[Any],
            await session.execute(
                update(ImportTask)
                .where(
                    ImportTask.id.in_(dead_task_ids),
                    ImportTask.task_status.in_(
                        (ImportStatus.QUEUED.value, ImportStatus.PROCESSING.value)
                    ),
                )
                .values(task_status=ImportStatus.FAILED.value, error_code="import_failed")
            ),
        )
        await session.commit()
    count = res.rowcount or 0
    if count:
        logger.warning("import_task.reconciled_dead", count=count)
    return count


class WorkerSettings:
    functions = [process_import_task]
