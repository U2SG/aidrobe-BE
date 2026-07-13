"""Phase E1 — hard-purge worker for deactivated accounts.

Consumes `async_jobs` rows of type=user_purge that the deactivate use case
enqueued with `next_run_at = deactivated_at + grace_days`. Runs on the
shared `job_runtime` so retry / lease / dead-letter all behave the same as
embedding/outfit_board.

What "hard purge" means here:
- DELETE the `users` row. FK ON DELETE CASCADE handles user_settings,
  user_sessions, wardrobe_items (and thence images, outfits via cascades)
- The `audit_log` table has NO FK on `user_id` on purpose — the audit
  trail of "we purged user X on 2026-Y-Z" survives the user's row going
  away. That row is what PIPL DSAR responses point at.
- We do NOT preserve a stub `users` row for "aggregates" right now. The
  current data model has no aggregate tables that survive user deletion;
  if Phase E later adds them, this worker grows a pre-delete snapshot
  step (and a tracking column on a new aggregates table).

Failure mode: if the cascade hits a transient lock or one of the worker
calls raises, mark_failed → retry with backoff → dead-letter. Sentry alert
already wired in Phase C4 will page oncall on user_purge dead-letters,
which matters because they map directly to a PIPL deletion-request SLA.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import delete

from app.application.audit import ACTION_USER_PURGE, ACTION_USER_PURGE_FAILED, record
from app.application.users.storage_purge import collect_user_object_keys, enqueue_storage_purge
from app.config.settings import get_settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.session import AsyncSessionLocal
from app.infrastructure.workers.job_runtime import (
    claim_one,
    mark_failed,
    mark_succeeded,
)

logger = structlog.get_logger(__name__)

JOB_TYPE = "user_purge"
# Bounded so a sweep of many expired-grace users in one tick doesn't lock
# the worker out of all other queues.
MAX_DRAIN_PER_TICK = 5


async def _process_one(job: AsyncJob) -> None:
    settings = get_settings()
    payload: dict[str, Any] = job.payload or {}
    user_id_str = payload.get("user_id")
    if not user_id_str:
        async with AsyncSessionLocal() as session:
            await mark_failed(
                session, job.id, claimed_attempt=job.attempts, error="missing user_id"
            )
            await session.commit()
        return

    user_id = uuid.UUID(user_id_str)
    structlog.contextvars.bind_contextvars(
        correlation_id=job.correlation_id or "",
        job_id=str(job.id),
        user_id=str(user_id),
    )
    try:
        if not settings.ENABLE_USER_PURGE:
            # Kill switch on. Re-queue with the runtime's normal backoff so
            # when oncall flips the flag back the job recovers automatically.
            async with AsyncSessionLocal() as session:
                await mark_failed(
                    session,
                    job.id,
                    claimed_attempt=job.attempts,
                    error="user_purge_disabled_by_flag",
                )
                await session.commit()
            logger.warning("user_purge.disabled_skipped", user_id=str(user_id))
            return

        try:
            async with AsyncSessionLocal() as session:
                # Confirm the user is still in deactivated state — a user who
                # reactivated within grace MUST NOT be purged.
                user = await session.get(User, user_id)
                if user is None:
                    # Already gone; treat as success (idempotent).
                    logger.info("user_purge.already_gone", user_id=str(user_id))
                elif user.deactivated_at is None:
                    # Reactivated. NOT an error — record + finalize succeeded.
                    logger.info("user_purge.cancelled_reactivated", user_id=str(user_id))
                    await record(
                        session,
                        actor="system",
                        action="user.purge_cancelled",
                        user_id=user_id,
                        target_type="user",
                        target_id=user_id,
                        payload={"reason": "user_reactivated_within_grace"},
                    )
                else:
                    # Collect storage keys BEFORE the cascade delete wipes the rows.
                    object_keys = await collect_user_object_keys(session, user_id, settings)
                    # Cascade-delete. FKs on user_settings / user_sessions /
                    # wardrobe_items / daily_recommendations are ON DELETE
                    # CASCADE so the children go too.
                    await session.execute(delete(User).where(User.id == user_id))
                    await record(
                        session,
                        actor="system",
                        action=ACTION_USER_PURGE,
                        user_id=user_id,
                        target_type="user",
                        target_id=user_id,
                    )
                    # Enqueue storage deletion in the same transaction so keys are
                    # durably scheduled even if the commit is the last thing that runs.
                    await enqueue_storage_purge(
                        session,
                        user_id=user_id,
                        keys=object_keys,
                        correlation_id=job.correlation_id,
                    )
                ok = await mark_succeeded(
                    session, job.id, claimed_attempt=job.attempts
                )
                if not ok:
                    await session.rollback()
                    logger.warning(
                        "user_purge.lease_lost_discarding", job_id=str(job.id)
                    )
                    return
                await session.commit()

        except Exception as exc:
            logger.exception("user_purge.failed", error=str(exc))
            async with AsyncSessionLocal() as session:
                new_status = await mark_failed(
                    session, job.id, claimed_attempt=job.attempts, error=str(exc)
                )
                if new_status == "dead":
                    await record(
                        session,
                        actor="system",
                        action=ACTION_USER_PURGE_FAILED,
                        user_id=user_id,
                        target_type="user",
                        target_id=user_id,
                        payload={"error": str(exc)[:500]},
                    )
                await session.commit()
            return
    finally:
        structlog.contextvars.unbind_contextvars("correlation_id", "job_id", "user_id")


async def drain_user_purge_jobs(ctx: object) -> int:
    processed = 0
    for _ in range(MAX_DRAIN_PER_TICK):
        async with AsyncSessionLocal() as session:
            job = await claim_one(session, [JOB_TYPE])
        if job is None:
            break
        await _process_one(job)
        processed += 1
    if processed:
        logger.info("user_purge.drain_completed", processed=processed)
    return processed
