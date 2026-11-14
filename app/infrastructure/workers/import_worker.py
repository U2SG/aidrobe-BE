"""Recoverable garment recognition, separate from the crop and paid-image jobs."""

import asyncio
import uuid
from typing import Any

import httpx
import structlog
from sqlalchemy import String, cast, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.import_tasks.candidates import SINGLE_CANDIDATE_ID, store_candidates
from app.application.import_tasks.failures import ImportFailure, public_error_message
from app.application.import_tasks.source import source_version, upload_key
from app.application.ports.vision import VisionResult
from app.bootstrap.container import object_storage, ocr_service, vision_rate_limiter, vision_service
from app.common.enums import ImportStatus, InputType
from app.common.exceptions import FriendlyException
from app.common.locale import resolve_locale
from app.config.settings import get_settings
from app.infrastructure.importers.external_http import InvalidExternalSource
from app.infrastructure.importers.ocr_router import ocr_adapter_for
from app.infrastructure.importers.router import RoutedLinkParser
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.import_task import ImportTask
from app.infrastructure.persistence.models.user import UserSettings
from app.infrastructure.persistence.repositories.import_task_repo import SqlAlchemyImportTaskRepository
from app.infrastructure.persistence.session import AsyncSessionLocal
from app.infrastructure.storage.source_image import (
    InvalidSourceImage,
    download_external_source_image,
    image_data_uri,
    load_source_bytes,
)
from app.infrastructure.vision.qwen_vision import NoGarmentsDetected
from app.infrastructure.workers.garment_crops import enqueue_garment_crop
from app.infrastructure.workers.job_runtime import (
    JOB_STATUS_DEAD,
    claim_is_current,
    claim_one,
    enqueue,
    mark_failed,
    mark_succeeded,
    recover_expired_leases,
    release_cancelled_claim,
)

logger = structlog.get_logger(__name__)

IMPORT_JOB_TYPE = "import_task"
IMPORT_MAX_DRAIN_PER_TICK = 10
IMPORT_ATTEMPT_TIMEOUT_SECONDS = 60
IMPORT_LEASE_SECONDS = IMPORT_ATTEMPT_TIMEOUT_SECONDS + 15
_ACTIVE = (ImportStatus.QUEUED.value, ImportStatus.PROCESSING.value)


def _candidate(result: VisionResult, url: str, candidate_id: str) -> dict[str, Any]:
    return {
        "title": result.display_name or result.title,
        "canonical_name": result.canonical_name or result.title,
        "display_name": result.display_name or result.title,
        "localized_names": result.localized_names,
        "category": result.category,
        "color_main": result.color_main,
        "confidence_score": result.confidence_score,
        "formality_score": result.formality_score,
        "season_tags": list(result.season_tags),
        "material": result.material,
        "style_tags": list(result.style_tags),
        "original_image_url": url,
        "temp_item_id": candidate_id,
        "should_import": True,
        "bbox": list(result.bbox) if result.bbox else None,
    }


async def _current_task(
    session: AsyncSession, task_id: uuid.UUID, user_id: uuid.UUID, generation: int, job: AsyncJob | None
) -> ImportTask | None:
    task = await SqlAlchemyImportTaskRepository(session).get_for_user(task_id, user_id, for_update=True)
    if task is None or task.run_generation != generation or task.task_status not in _ACTIVE:
        return None
    if job is not None and not await claim_is_current(session, job):
        return None
    return task


async def _checkpoint_source(
    session: AsyncSession,
    task_id: uuid.UUID,
    user_id: uuid.UUID,
    generation: int,
    payload: dict[str, Any],
    job: AsyncJob | None,
) -> tuple[str, str, bytes] | None:
    settings = get_settings()
    raw_url = payload.get("image_url")
    url = raw_url if isinstance(raw_url, str) else None
    raw_key = payload.get("source_object_key")
    key = raw_key if isinstance(raw_key, str) else None
    if not (key or url) and isinstance(payload.get("source_url"), str):
        url = await RoutedLinkParser(settings, strict=True).extract_image_url(payload["source_url"])
    if not url:
        raise InvalidSourceImage("Missing image reference")
    key = upload_key(url, user_id, expected_key=key)

    if key:
        data = await load_source_bytes(url, key, user_id=user_id)
        # Legacy references may contain expired signatures; the key is authoritative.
        url = url.split("?", 1)[0].split("#", 1)[0]
    else:
        data, mime = await download_external_source_image(url, settings)
        # A late attempt may finish writing after losing the claim. Unique
        # object names keep its bytes away from the adopted snapshot.
        key = f"users/{user_id}/wardrobe/imports/{task_id}/{uuid.uuid4().hex}.{mime.split('/')[-1]}"
        url = await object_storage().put_object(key, data, mime)

    if key != payload.get("source_object_key") or url != payload.get("image_url"):
        task = await _current_task(session, task_id, user_id, generation, job)
        if task is None:
            await session.rollback()
            return None
        task.input_payload = {**task.input_payload, "source_object_key": key, "image_url": url}
        await session.commit()
    return url, key, data


def _classify(exc: Exception, stage: str) -> ImportFailure:
    if isinstance(exc, ImportFailure):
        return exc
    if isinstance(exc, TimeoutError):
        return ImportFailure("import_attempt_timed_out", True, stage)
    if isinstance(exc, NoGarmentsDetected):
        return ImportFailure("no_garments", False, stage)
    if isinstance(exc, InvalidSourceImage | InvalidExternalSource | httpx.InvalidURL):
        return ImportFailure("image_invalid" if stage == "decode" else "source_invalid", False, stage)
    if isinstance(exc, httpx.RequestError | OSError):
        if isinstance(exc, FileNotFoundError):
            return ImportFailure("source_invalid", False, stage)
        return ImportFailure("provider_unavailable", True, stage)
    if isinstance(exc, httpx.HTTPStatusError | FriendlyException):
        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else exc.status_code
        if status == 429 or status >= 500:
            return ImportFailure("provider_unavailable", True, stage, status)
        if stage == "source":
            return ImportFailure("source_invalid", False, stage, status)
        if status in (401, 403, 404):
            return ImportFailure("provider_configuration", False, stage, status)
        if isinstance(exc, httpx.HTTPStatusError) and status in (400, 422):
            try:
                error = exc.response.json()
                error = error.get("error", error) if isinstance(error, dict) else {}
                code = error.get("code") if isinstance(error, dict) else None
            except ValueError:
                code = None
            if code in ("InvalidImage", "InvalidImageFormat", "invalid_image", "invalid_image_format"):
                return ImportFailure("image_invalid", False, stage, status)
            return ImportFailure("provider_configuration", False, stage, status)
    if stage == "provider_setup" and isinstance(exc, ValueError):
        return ImportFailure("provider_configuration", False, stage)
    if stage == "source":
        # S3 and OSS expose HTTP status through their service errors.
        storage_status = getattr(exc, "status", None)
        response = getattr(exc, "response", None)
        if isinstance(response, dict):
            storage_status = response.get("ResponseMetadata", {}).get("HTTPStatusCode")
        if isinstance(storage_status, int):
            if storage_status == 404:
                return ImportFailure("source_invalid", False, stage, storage_status)
            if storage_status == 429 or storage_status >= 500:
                return ImportFailure("provider_unavailable", True, stage, storage_status)
            if storage_status in (401, 403):
                return ImportFailure("provider_configuration", False, stage, storage_status)
    if stage in ("vision", "ocr") and isinstance(exc, ValueError | TypeError | KeyError | IndexError):
        return ImportFailure("provider_response_invalid", True, stage)
    return ImportFailure("import_failed", True, stage)


async def _recognize(
    session: AsyncSession, task_id: uuid.UUID, user_id: uuid.UUID, generation: int, job: AsyncJob | None
) -> None:
    task = await _current_task(session, task_id, user_id, generation, job)
    if task is None:
        if job is not None and await claim_is_current(session, job):
            await mark_succeeded(session, job.id, claimed_attempt=job.attempts, result={"outcome": "obsolete"})
            await session.commit()
        else:
            await session.rollback()
        return
    if task.candidate_payload:
        # Preserve an already committed checkpoint even if its finalizer died.
        task.task_status = ImportStatus.WAITING_CONFIRM.value
        if job is not None:
            await mark_succeeded(session, job.id, claimed_attempt=job.attempts, result={"outcome": "checkpoint"})
        await session.commit()
        return
    task.task_status = ImportStatus.PROCESSING.value
    task.error_code = None
    task.error_message = None
    task.retryable = None
    payload = dict(task.input_payload)
    input_type = task.input_type
    user_locale = await session.scalar(select(UserSettings.locale).where(UserSettings.user_id == user_id))
    request_locale = payload.get("locale")
    locale = resolve_locale(request_locale if isinstance(request_locale, str) else None, user_locale)
    await session.commit()

    stage = "source"
    try:
        source = await _checkpoint_source(session, task_id, user_id, generation, payload, job)
        if source is None:
            return
        url, key, data = source
        stage = "decode"
        image = image_data_uri(data)
        stage = "provider_setup"
        adapter = ocr_adapter_for(input_type)
        if adapter is not None:
            ocr = ocr_service()
            stage = "ocr"
            vision_rate_limiter().check(user_id)
            result = await ocr.extract_text(image)
            if not result.title.strip():
                raise NoGarmentsDetected
            candidates: list[dict[str, Any]] = [
                {
                    **adapter.to_candidate_payload(result, url),
                    "temp_item_id": SINGLE_CANDIDATE_ID,
                    "should_import": True,
                }
            ]
        else:
            vision = vision_service()
            stage = "vision"
            vision_rate_limiter().check(user_id)
            if input_type == InputType.OUTFIT_IMAGE:
                results = await vision.analyze_outfit(image, locale)
            else:
                results = [await vision.analyze_image(image, locale)]
            if not results:
                raise NoGarmentsDetected
            candidates = [
                _candidate(result, url, uuid.uuid5(task_id, str(index)).hex)
                for index, result in enumerate(results)
            ]
    except Exception as exc:
        raise _classify(exc, stage) from None

    task = await _current_task(session, task_id, user_id, generation, job)
    if task is None:
        await session.rollback()
        return
    version = source_version(key, url)
    task.input_payload = {**task.input_payload, "source_version": version}
    store_candidates(task, candidates)
    task.task_status = ImportStatus.WAITING_CONFIRM.value
    if input_type == InputType.OUTFIT_IMAGE:
        for candidate in candidates:
            if candidate.get("bbox") and candidate.get("should_import") is not False:
                await enqueue_garment_crop(session, task, candidate["temp_item_id"], version, job)
    if job is not None:
        await mark_succeeded(
            session, job.id, claimed_attempt=job.attempts, result={"outcome": "recognized", "count": len(candidates)},
        )
    await session.commit()


async def _record_failure(
    session: AsyncSession,
    task_id: uuid.UUID,
    user_id: uuid.UUID,
    generation: int,
    failure: ImportFailure,
    job: AsyncJob | None,
) -> None:
    task = await _current_task(session, task_id, user_id, generation, job)
    if task is None:
        await session.rollback()
        return
    if job is not None:
        if failure.retryable:
            status = await mark_failed(session, job.id, claimed_attempt=job.attempts, error=failure.code)
            if status != JOB_STATUS_DEAD:
                await session.commit()
                return
        else:
            await mark_succeeded(
                session, job.id, claimed_attempt=job.attempts,
                result={"outcome": "rejected", "error_code": failure.code, "stage": failure.stage},
            )
    task.task_status = ImportStatus.FAILED.value
    task.error_code = failure.code
    task.error_message = public_error_message(failure.code)
    task.retryable = failure.retryable
    await session.commit()


async def _run_import(
    task_id: uuid.UUID, user_id: uuid.UUID, generation: int, job: AsyncJob | None = None
) -> None:
    with structlog.contextvars.bound_contextvars(
        correlation_id=job.correlation_id if job else None,
        job_id=str(job.id) if job else None,
        task_id=str(task_id),
    ):
        try:
            async with asyncio.timeout(IMPORT_ATTEMPT_TIMEOUT_SECONDS), AsyncSessionLocal() as session:
                await _recognize(session, task_id, user_id, generation, job)
        except asyncio.CancelledError:
            if job is not None:
                await release_cancelled_claim(job, AsyncSessionLocal)
            raise
        except Exception as exc:
            failure = _classify(exc, "recognition")
            logger.warning(
                "import_task.attempt_failed",
                error_code=failure.code, stage=failure.stage, provider_status=failure.provider_status,
                retryable=failure.retryable,
            )
            async with AsyncSessionLocal() as session:
                await _record_failure(session, task_id, user_id, generation, failure, job)


async def process_import_task(ctx: object, task_id: str, user_id: str, generation: int = 0) -> None:
    """Legacy arq / BackgroundTasks entry point opens an independent session."""
    await _run_import(uuid.UUID(task_id), uuid.UUID(user_id), generation)


async def enqueue_import_task(
    session: AsyncSession,
    *,
    task_id: uuid.UUID,
    user_id: uuid.UUID,
    correlation_id: str | None = None,
    generation: int = 0,
) -> tuple[AsyncJob, bool]:
    key = f"import_task:{task_id}"
    return await enqueue(
        session,
        job_type=IMPORT_JOB_TYPE,
        idempotency_key=f"{key}:{generation}" if generation else key,
        payload={"task_id": str(task_id), "user_id": str(user_id), "generation": generation},
        user_id=user_id,
        target_id=task_id,
        correlation_id=correlation_id,
    )


async def _process_one_import(job: AsyncJob) -> None:
    payload = job.payload or {}
    try:
        task_id = uuid.UUID(payload["task_id"])
        user_id = uuid.UUID(payload["user_id"])
        generation = int(payload.get("generation", 0))
    except (KeyError, ValueError, TypeError, AttributeError):
        async with AsyncSessionLocal() as session:
            await mark_failed(session, job.id, claimed_attempt=job.attempts, error="missing payload fields")
            await session.commit()
        return
    await _run_import(task_id, user_id, generation, job)


async def drain_import_jobs(ctx: object) -> int:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + get_settings().ARQ_JOB_TIMEOUT_SECONDS - 5
    async with AsyncSessionLocal() as session:
        recovered = await recover_expired_leases(session, job_type=IMPORT_JOB_TYPE)
        await session.commit()
    if recovered:
        await reconcile_dead_import_tasks(ctx)

    processed = 0
    while processed < IMPORT_MAX_DRAIN_PER_TICK and deadline - loop.time() >= IMPORT_LEASE_SECONDS:
        async with AsyncSessionLocal() as session:
            job = await claim_one(session, [IMPORT_JOB_TYPE], lease_seconds=IMPORT_LEASE_SECONDS)
        if job is None:
            break
        await _process_one_import(job)
        processed += 1
    return processed


async def reconcile_dead_import_tasks(ctx: object) -> int:
    """Only the dead job of the current run may end an unfinished import."""
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                update(ImportTask)
                .where(
                    ImportTask.task_status.in_(_ACTIVE),
                    select(AsyncJob.id)
                    .where(
                        AsyncJob.job_type == IMPORT_JOB_TYPE,
                        AsyncJob.status == JOB_STATUS_DEAD,
                        AsyncJob.target_id == ImportTask.id,
                        func.coalesce(AsyncJob.payload["generation"].astext, "0")
                        == cast(ImportTask.run_generation, String),
                    )
                    .exists(),
                )
                .values(
                    task_status=ImportStatus.FAILED.value, error_code="import_failed",
                    error_message=public_error_message("import_failed"), retryable=True,
                )
                .returning(ImportTask.id)
            )
        ).scalars().all()
        await session.commit()
    return len(row)


class WorkerSettings:
    functions = [process_import_task]
