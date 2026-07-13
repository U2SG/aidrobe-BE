"""Storage-object deletion worker for PIPL compliance.

Consumes `async_jobs` rows of type=storage_purge enqueued by
`enqueue_storage_purge`. Each job carries a list of object keys (already
collected before the DB rows were deleted) and delegates to the configured
`ObjectStoragePort.delete_objects`.

`delete_objects` is idempotent — missing keys are success. So retries are
safe even if a partial deletion already happened on a previous attempt.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog

from app.bootstrap.container import object_storage
from app.config.settings import get_settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.session import AsyncSessionLocal
from app.infrastructure.workers.job_runtime import (
    claim_one,
    mark_failed,
    mark_succeeded,
)

logger = structlog.get_logger(__name__)

JOB_TYPE = "storage_purge"
MAX_DRAIN_PER_TICK = 5


async def _process_one(job: AsyncJob) -> None:
    payload: dict[str, Any] = job.payload or {}
    user_id_str = payload.get("user_id", "")
    keys: list[str] = payload.get("keys") or []

    structlog.contextvars.bind_contextvars(
        correlation_id=job.correlation_id or "",
        job_id=str(job.id),
        user_id=user_id_str,
    )
    try:
        if not user_id_str:
            async with AsyncSessionLocal() as session:
                await mark_failed(
                    session, job.id, claimed_attempt=job.attempts, error="missing user_id"
                )
                await session.commit()
            return

        get_settings()  # eager-validate config

        try:
            storage = object_storage()
            await storage.delete_objects(keys)
            # Catch-all sweep: exports, STS uploads into per-purpose buckets,
            # and anything whose DB row disappeared before key collection all
            # live under the user's prefix. UUID-parse guards against ever
            # issuing a root/short prefix delete from a malformed payload.
            prefix = f"users/{uuid.UUID(user_id_str)}/"
            swept = await storage.delete_prefix(prefix)
            logger.info(
                "storage_purge.deleted",
                user_id=user_id_str,
                keys_count=len(keys),
                prefix_swept=swept,
            )

            async with AsyncSessionLocal() as session:
                ok = await mark_succeeded(session, job.id, claimed_attempt=job.attempts)
                if not ok:
                    await session.rollback()
                    logger.warning(
                        "storage_purge.lease_lost_discarding", job_id=str(job.id)
                    )
                    return
                await session.commit()

        except Exception as exc:
            logger.exception("storage_purge.failed", error=str(exc))
            async with AsyncSessionLocal() as session:
                await mark_failed(
                    session, job.id, claimed_attempt=job.attempts, error=str(exc)
                )
                await session.commit()
    finally:
        structlog.contextvars.unbind_contextvars("correlation_id", "job_id", "user_id")


async def drain_storage_purge_jobs(ctx: object) -> int:
    processed = 0
    for _ in range(MAX_DRAIN_PER_TICK):
        async with AsyncSessionLocal() as session:
            job = await claim_one(session, [JOB_TYPE])
        if job is None:
            break
        await _process_one(job)
        processed += 1
    if processed:
        logger.info("storage_purge.drain_completed", processed=processed)
    return processed
