"""Receipt OCR worker — Phase M18.

Drains ``ocr_receipt`` async jobs enqueued by the receipt / order-screenshot /
shopping-email import pipeline. Each job carries a ``task_id`` and ``user_id``
in its payload; the worker fetches the matching ``import_task`` row, runs OCR
via the configured :class:`~app.application.ports.ocr.OcrPort`, writes the
structured candidate payload back, and advances the task status to
``waiting_confirm``.

Worker structure mirrors :mod:`app.infrastructure.workers.embedding_worker`:
one ``drain_*`` arq entrypoint, one ``_process_one`` internal function, and
``MAX_DRAIN_PER_TICK`` to bound latency impact on other queues.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog

from app.infrastructure.persistence.session import AsyncSessionLocal
from app.infrastructure.workers.job_runtime import (
    claim_one,
    mark_failed,
    mark_succeeded,
)

logger = structlog.get_logger(__name__)

JOB_TYPE = "ocr_receipt"
MAX_DRAIN_PER_TICK = 10


async def _process_one_job(job_id: uuid.UUID, attempts: int, payload: dict[str, Any]) -> None:
    """Run one claimed ocr_receipt job end-to-end.

    Uses separate short sessions for claim/finalize so a crash between them
    doesn't poison the next claim (mirrors embedding_worker pattern).
    """
    from app.bootstrap.container import ocr_service
    from app.infrastructure.importers.ocr_router import ocr_adapter_for
    from app.infrastructure.persistence.repositories.import_task_repo import (
        SqlAlchemyImportTaskRepository,
    )

    task_id_str = payload.get("task_id")
    user_id_str = payload.get("user_id")
    if not task_id_str or not user_id_str:
        async with AsyncSessionLocal() as session:
            await mark_failed(
                session, job_id, claimed_attempt=attempts, error="missing payload fields"
            )
            await session.commit()
        return

    task_id = uuid.UUID(task_id_str)
    user_id = uuid.UUID(user_id_str)

    structlog.contextvars.bind_contextvars(
        job_id=str(job_id),
        task_id=str(task_id),
        user_id=str(user_id),
    )
    try:
        async with AsyncSessionLocal() as session:
            repo = SqlAlchemyImportTaskRepository(session)
            task = await repo.get_for_user(task_id, user_id)
            if task is None:
                logger.warning("receipt_worker.task_not_found", task_id=str(task_id))
                await mark_failed(
                    session, job_id, claimed_attempt=attempts, error="import_task not found"
                )
                await session.commit()
                return

            input_payload: dict[str, Any] = task.input_payload or {}
            image_url: str = input_payload.get("image_url") or ""
            if not image_url:
                logger.warning("receipt_worker.no_image_url", task_id=str(task_id))
                await mark_failed(
                    session, job_id, claimed_attempt=attempts, error="no image_url in input_payload"
                )
                await session.commit()
                return

            adapter = ocr_adapter_for(task.input_type)
            if adapter is None:
                logger.warning(
                    "receipt_worker.no_adapter",
                    task_id=str(task_id),
                    input_type=task.input_type,
                )
                await mark_failed(
                    session,
                    job_id,
                    claimed_attempt=attempts,
                    error=f"no OCR adapter for input_type={task.input_type}",
                )
                await session.commit()
                return

            try:
                ocr = ocr_service()
                ocr_result = await ocr.extract_text(image_url)
            except Exception as exc:
                logger.warning("receipt_worker.ocr_failed", task_id=str(task_id), error=str(exc))
                from app.common.enums import ImportStatus

                task.task_status = ImportStatus.FAILED.value
                task.error_code = "ocr_failed"
                task.error_message = str(exc)
                await repo.flush()
                await mark_failed(session, job_id, claimed_attempt=attempts, error=str(exc))
                await session.commit()
                return

            from app.common.enums import ImportStatus

            task.task_status = ImportStatus.WAITING_CONFIRM.value
            task.candidate_payload = adapter.to_candidate_payload(ocr_result, image_url)
            await repo.flush()

            ok = await mark_succeeded(
                session, job_id, claimed_attempt=attempts, cost_tokens=0, cost_cents=0
            )
            if not ok:
                await session.rollback()
                logger.warning(
                    "receipt_worker.lease_lost_discarding_result",
                    job_id=str(job_id),
                )
                return
            await session.commit()
            logger.info("receipt_worker.succeeded", task_id=str(task_id))
    finally:
        structlog.contextvars.unbind_contextvars("job_id", "task_id", "user_id")


async def drain_receipt_jobs(ctx: object) -> int:
    """arq entrypoint. Drains up to ``MAX_DRAIN_PER_TICK`` ocr_receipt jobs.

    Returns the number of jobs processed for observability."""
    processed = 0
    for _ in range(MAX_DRAIN_PER_TICK):
        async with AsyncSessionLocal() as session:
            job = await claim_one(session, [JOB_TYPE])
        if job is None:
            break
        await _process_one_job(job.id, job.attempts, job.payload or {})
        processed += 1
    if processed:
        logger.info("receipt.drain_completed", processed=processed)
    return processed
