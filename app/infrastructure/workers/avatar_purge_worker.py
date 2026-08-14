"""V3-013 — delete avatar objects from storage, out of band.

Consumes `async_jobs` rows of type `avatar_purge` enqueued by
`app.application.avatar.delete_avatar`. The rows are already gone when this
runs; this is the storage half of "Delete removes or schedules removal of
derived assets" (V3-013 acceptance 3).

**Why it is a job and not part of the request.** Object deletion is neither
transactional nor reversible. Running it inline would mean a rollback after a
successful `delete_objects` leaves the DB rows intact and the images destroyed —
an avatar that exists but is broken, and unrecoverable. A job also retries,
which is the right trade-off here specifically: a dropped purge leaves a
person's photos and canonical renders in the bucket after they asked for
deletion. That is why `MAX_ATTEMPTS` is 3 here and 1 for `avatar_candidate`,
where a lost job leaves nothing behind at all.

**Retry safety is inherited, not invented.** `ObjectStoragePort.delete_objects`
is documented idempotent ("already-deleted or missing keys are treated as
success"), so a retry after a partial success finishes the job instead of
failing on the keys the first attempt already removed.

**No dead-letter reconciler.** `outfit_board` and `cutout` have one because a
dead job leaves a user-visible row stuck in a non-terminal state. Here the
user-visible outcome — the avatar is gone — has already happened in the request.
A dead purge job is an operational leak, and `job_runtime._alert_dead` already
raises a Sentry event for it, which is the channel that can actually act on it.
"""

from __future__ import annotations

from typing import Any

import structlog

from app.bootstrap.container import object_storage
from app.infrastructure.persistence.session import AsyncSessionLocal
from app.infrastructure.workers.job_runtime import (
    claim_one,
    mark_failed,
    mark_succeeded,
)

logger = structlog.get_logger(__name__)

JOB_TYPE = "avatar_purge"
MAX_DRAIN_PER_TICK = 5


async def _process_one(job: Any) -> None:
    payload: dict[str, Any] = job.payload or {}
    raw_keys = payload.get("object_keys") or []
    structlog.contextvars.bind_contextvars(
        correlation_id=job.correlation_id or "",
        job_id=str(job.id),
        user_id=payload.get("user_id", ""),
    )
    try:
        keys = [key for key in raw_keys if isinstance(key, str) and key]
        if not keys:
            # Nothing to delete is a success, not a failure: the deletion path
            # only enqueues when it has keys, so this is a replay or a payload
            # that lost its list, and failing it would page someone over an
            # empty sweep.
            async with AsyncSessionLocal() as session:
                await mark_succeeded(session, job.id, claimed_attempt=job.attempts)
                await session.commit()
            return

        try:
            await object_storage().delete_objects(keys)
        except Exception as exc:
            # Only the exception type is persisted. A storage SDK error commonly
            # quotes the failing key, and `async_jobs.error` outlives log
            # retention — this job's payload is a list of object keys, so this is
            # exactly the exposure V3-012's round-3 review found on
            # `avatar_candidate`. The traceback still reaches Sentry, where an
            # operator needs it.
            logger.exception("avatar.purge.failed", error_type=type(exc).__name__)
            async with AsyncSessionLocal() as session:
                await mark_failed(
                    session, job.id, claimed_attempt=job.attempts, error=type(exc).__name__
                )
                await session.commit()
            return

        async with AsyncSessionLocal() as session:
            ok = await mark_succeeded(session, job.id, claimed_attempt=job.attempts)
            if not ok:
                await session.rollback()
                logger.warning("avatar.purge.lease_lost_discarding")
                return
            await session.commit()

        # A count and the user's choice — never the keys themselves (contract §7).
        logger.info(
            "avatar.purge.completed",
            object_count=len(keys),
            purge_derived=bool(payload.get("purge_derived")),
        )
    finally:
        structlog.contextvars.unbind_contextvars("correlation_id", "job_id", "user_id")


async def drain_avatar_purge_jobs(ctx: object) -> int:
    processed = 0
    for _ in range(MAX_DRAIN_PER_TICK):
        async with AsyncSessionLocal() as session:
            job = await claim_one(session, [JOB_TYPE])
        if job is None:
            break
        await _process_one(job)
        processed += 1
    if processed:
        logger.info("avatar.purge.drain_completed", processed=processed)
    return processed
