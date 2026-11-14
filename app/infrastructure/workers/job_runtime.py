"""Generic async-job runtime backed by the `async_jobs` table.

Contract — all V3+ async pipelines (embedding, outfit_board, llm_ask, ocr,
pdf_ingest, tryon, user_purge) go through this module:

- `enqueue` is idempotent on `idempotency_key`. Duplicate enqueues return the
  existing row instead of inserting a new one. No double charging, no
  double work.
- `claim_one` uses `SELECT ... FOR UPDATE SKIP LOCKED` so any number of
  workers can race over the same queue without double-pickup. **It commits
  the claim before returning** so the row is durably `running` with an
  attempts increment and a lease — caller crashing mid-job no longer rolls
  the claim back to `queued` (which would erase backoff/retry accounting).
- `mark_succeeded` / `mark_failed` are guarded by `status='running'`. A stale
  finalizer cannot resurrect a terminal job or finalize an unclaimed row.
- `recover_expired_leases` returns rows whose `lease_expires_at` has passed
  back to `queued` so the next sweep picks them up. This is the only path
  through which a crashed/killed worker's job re-enters the queue.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable, Sequence
from datetime import UTC, datetime, timedelta
from typing import Any, cast

import sentry_sdk
import structlog
from sqlalchemy import CursorResult, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.session import AsyncSessionLocal

logger = structlog.get_logger(__name__)


def _alert_dead(
    *,
    job_id: uuid.UUID,
    job_type: str,
    attempts: int,
    max_attempts: int,
    correlation_id: str | None,
    error: str | None,
    cause: str,
) -> None:
    """Page oncall when a job lands in `dead`. Safe even when Sentry is not
    configured — the SDK no-ops without a DSN."""
    try:
        # Tags/extras MUST be set on the scope BEFORE capture_message — otherwise
        # the event lands in Sentry with no metadata and oncall has nothing to
        # triage on.
        with sentry_sdk.push_scope() as scope:
            scope.set_tag("job_type", job_type)
            scope.set_tag("dead_cause", cause)
            scope.set_extra("job_id", str(job_id))
            scope.set_extra("attempts", attempts)
            scope.set_extra("max_attempts", max_attempts)
            scope.set_extra("correlation_id", correlation_id)
            scope.set_extra("error", error)
            sentry_sdk.capture_message(
                f"async_job.dead: {job_type}",
                level="error",
            )
    except Exception:
        # Never let observability take down the worker.
        logger.exception("sentry_capture_failed", job_id=str(job_id))


# Exponential backoff schedule, in seconds. Index = attempts already made.
# After `max_attempts` failures the row goes to `dead` regardless.
_BACKOFF_SECONDS: tuple[int, ...] = (60, 300, 1800, 7200)

# Default lease length. Long enough for slow LLM/embedding calls; short enough
# that a hard-killed worker doesn't pin a job for hours.
DEFAULT_LEASE_SECONDS = 600

JOB_STATUS_QUEUED = "queued"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_SUCCEEDED = "succeeded"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_DEAD = "dead"


def _backoff_for(attempts: int) -> int:
    """Return seconds to wait before retrying after `attempts` failures.

    `attempts` is the number of failures *already recorded*, so calling
    `_backoff_for(1)` after the first failure returns the wait until retry #2.
    """
    if attempts <= 0:
        return _BACKOFF_SECONDS[0]
    idx = min(attempts - 1, len(_BACKOFF_SECONDS) - 1)
    return _BACKOFF_SECONDS[idx]


async def enqueue(
    session: AsyncSession,
    *,
    job_type: str,
    idempotency_key: str,
    payload: dict[str, Any] | None = None,
    user_id: uuid.UUID | None = None,
    target_id: uuid.UUID | None = None,
    correlation_id: str | None = None,
    max_attempts: int = 3,
) -> tuple[AsyncJob, bool]:
    """Insert a new job or return the existing one with the same
    `idempotency_key`. The boolean is True iff a new row was inserted.

    Caller owns transaction commit; this only flushes.
    """
    if max_attempts < 1:
        raise ValueError("max_attempts must be >= 1")

    stmt = (
        pg_insert(AsyncJob)
        .values(
            job_type=job_type,
            idempotency_key=idempotency_key,
            user_id=user_id,
            target_id=target_id,
            payload=payload,
            correlation_id=correlation_id,
            max_attempts=max_attempts,
        )
        .on_conflict_do_nothing(index_elements=["idempotency_key"])
        .returning(AsyncJob.id)
    )
    new_id = (await session.execute(stmt)).scalar_one_or_none()
    if new_id is not None:
        await session.flush()
        row = await session.get(AsyncJob, new_id)
        assert row is not None  # just inserted
        logger.info(
            "job.enqueued",
            job_id=str(row.id),
            job_type=job_type,
            idempotency_key=idempotency_key,
            correlation_id=correlation_id,
        )
        return row, True

    existing_row = (
        await session.execute(select(AsyncJob).where(AsyncJob.idempotency_key == idempotency_key))
    ).scalar_one()
    logger.info(
        "job.enqueue_idempotent_hit",
        job_id=str(existing_row.id),
        job_type=job_type,
        idempotency_key=idempotency_key,
    )
    return existing_row, False


async def claim_one(
    session: AsyncSession,
    job_types: Sequence[str],
    *,
    now: datetime | None = None,
    lease_seconds: int = DEFAULT_LEASE_SECONDS,
) -> AsyncJob | None:
    """Atomically claim the oldest queued job whose `next_run_at <= now` and
    **commit** the claim before returning.

    Why commit-on-claim: if the caller does external work (LLM call, S3 fetch)
    and crashes before `mark_succeeded`/`mark_failed`, an uncommitted claim
    rolls back to `queued` with `attempts` unchanged — the next worker would
    repeat the job and burn cost. Committing immediately means the only
    recovery path for a crashed worker is the lease-expiry sweep, which goes
    through `recover_expired_leases` and preserves the attempts counter.

    Uses `FOR UPDATE SKIP LOCKED` so concurrent workers never pick the same
    row. The returned `AsyncJob` is detached from the session — read its
    fields, do work, then call `mark_succeeded`/`mark_failed` with `job.id`.
    """
    if not job_types:
        return None
    now = now or datetime.now(UTC)
    lease_until = now + timedelta(seconds=lease_seconds)

    select_stmt = (
        select(AsyncJob)
        .where(
            AsyncJob.status == JOB_STATUS_QUEUED,
            AsyncJob.next_run_at <= now,
            AsyncJob.job_type.in_(list(job_types)),
        )
        .order_by(AsyncJob.next_run_at.asc(), AsyncJob.created_at.asc())
        .limit(1)
        .with_for_update(skip_locked=True)
    )
    job = (await session.execute(select_stmt)).scalar_one_or_none()
    if job is None:
        return None

    await session.execute(
        update(AsyncJob)
        .where(AsyncJob.id == job.id, AsyncJob.status == JOB_STATUS_QUEUED)
        .values(
            status=JOB_STATUS_RUNNING,
            attempts=AsyncJob.attempts + 1,
            lease_expires_at=lease_until,
            updated_at=now,
        )
    )
    await session.commit()
    # Re-load post-commit so the caller sees the running state + new attempts.
    refreshed = await session.get(AsyncJob, job.id)
    assert refreshed is not None
    logger.info(
        "job.started",
        job_id=str(refreshed.id),
        job_type=refreshed.job_type,
        attempts=refreshed.attempts,
        lease_expires_at=lease_until.isoformat(),
        correlation_id=refreshed.correlation_id,
    )
    return refreshed


async def claim_is_current(session: AsyncSession, job: AsyncJob) -> bool:
    """Lock the job while deciding whether a business write may commit."""
    return (
        await session.scalar(
            select(AsyncJob.id)
            .where(
                AsyncJob.id == job.id,
                AsyncJob.status == JOB_STATUS_RUNNING,
                AsyncJob.attempts == job.attempts,
                AsyncJob.lease_expires_at > datetime.now(UTC),
            )
            .with_for_update()
        )
    ) is not None


async def release_cancelled_claim(
    job: AsyncJob, session_factory: Callable[[], AsyncSession] = AsyncSessionLocal
) -> None:
    """Best effort within the cancellation window, backed by lease recovery."""
    try:
        async with asyncio.timeout(4), session_factory() as session:
            await mark_failed(session, job.id, claimed_attempt=job.attempts, error="attempt_cancelled")
            await session.commit()
    except Exception:
        logger.warning("job.cancel_cleanup_failed", job_id=str(job.id))


async def mark_succeeded(
    session: AsyncSession,
    job_id: uuid.UUID,
    *,
    claimed_attempt: int,
    result: dict[str, Any] | None = None,
    cost_tokens: int = 0,
    cost_cents: int = 0,
) -> bool:
    """Finalize a running job as succeeded. Returns True if the transition
    happened, False if the row was no longer running OR has been re-claimed
    on a later attempt (stale finalizer / lease recovered + reclaimed).

    `claimed_attempt` is the `attempts` value `claim_one` returned to the
    caller — it acts as the ownership fence. After lease recovery + a fresh
    claim by another worker, `attempts` has incremented, so a stale worker's
    finalizer no longer matches and cannot commit its work over the new
    claim's in-flight state."""
    now = datetime.now(UTC)
    res = cast(
        CursorResult[Any],
        await session.execute(
            update(AsyncJob)
            .where(
                AsyncJob.id == job_id,
                AsyncJob.status == JOB_STATUS_RUNNING,
                AsyncJob.attempts == claimed_attempt,
            )
            .values(
                status=JOB_STATUS_SUCCEEDED,
                result=result,
                cost_tokens=AsyncJob.cost_tokens + cost_tokens,
                cost_cents=AsyncJob.cost_cents + cost_cents,
                last_error=None,
                lease_expires_at=None,
                updated_at=now,
            )
        ),
    )
    if res.rowcount != 1:
        logger.warning(
            "job.finalize_conflict",
            job_id=str(job_id),
            target_status=JOB_STATUS_SUCCEEDED,
            claimed_attempt=claimed_attempt,
            rowcount=res.rowcount,
        )
        return False
    logger.info(
        "job.succeeded",
        job_id=str(job_id),
        cost_tokens=cost_tokens,
        cost_cents=cost_cents,
    )
    return True


async def mark_failed(
    session: AsyncSession,
    job_id: uuid.UUID,
    *,
    claimed_attempt: int,
    error: str,
    result: dict[str, Any] | None = None,
) -> str | None:
    """Move a running job back to queued with backoff, or to dead if attempts
    have hit max_attempts. Returns the new status, or None if the row was no
    longer `running` on the claimed attempt (stale finalizer / re-claimed by
    another worker after lease recovery — see `claimed_attempt` semantics in
    `mark_succeeded`)."""
    now = datetime.now(UTC)
    job = await session.get(AsyncJob, job_id)
    if job is None:
        raise LookupError(f"async_job {job_id} not found")
    if job.status != JOB_STATUS_RUNNING or job.attempts != claimed_attempt:
        logger.warning(
            "job.finalize_conflict",
            job_id=str(job_id),
            target_status="failed",
            current_status=job.status,
            current_attempts=job.attempts,
            claimed_attempt=claimed_attempt,
        )
        return None
    truncated = (error or "")[:4000]
    result_update = {"result": result} if result is not None else {}
    if job.attempts >= job.max_attempts:
        res = cast(
            CursorResult[Any],
            await session.execute(
                update(AsyncJob)
                .where(
                    AsyncJob.id == job_id,
                    AsyncJob.status == JOB_STATUS_RUNNING,
                    AsyncJob.attempts == claimed_attempt,
                )
                .values(
                    status=JOB_STATUS_DEAD,
                    last_error=truncated,
                    lease_expires_at=None,
                    updated_at=now,
                    **result_update,
                )
            ),
        )
        if res.rowcount != 1:
            logger.warning(
                "job.finalize_conflict",
                job_id=str(job_id),
                target_status=JOB_STATUS_DEAD,
                claimed_attempt=claimed_attempt,
                rowcount=res.rowcount,
            )
            return None
        logger.error(
            "job.dead",
            job_id=str(job_id),
            job_type=job.job_type,
            attempts=job.attempts,
            max_attempts=job.max_attempts,
            correlation_id=job.correlation_id,
            error=truncated[:500],
        )
        _alert_dead(
            job_id=job_id,
            job_type=job.job_type,
            attempts=job.attempts,
            max_attempts=job.max_attempts,
            correlation_id=job.correlation_id,
            error=truncated[:500],
            cause="max_attempts",
        )
        return JOB_STATUS_DEAD

    next_run = now + timedelta(seconds=_backoff_for(job.attempts))
    res = cast(
        CursorResult[Any],
        await session.execute(
            update(AsyncJob)
            .where(
                AsyncJob.id == job_id,
                AsyncJob.status == JOB_STATUS_RUNNING,
                AsyncJob.attempts == claimed_attempt,
            )
            .values(
                status=JOB_STATUS_QUEUED,
                last_error=truncated,
                next_run_at=next_run,
                lease_expires_at=None,
                updated_at=now,
                **result_update,
            )
        ),
    )
    if res.rowcount != 1:
        logger.warning(
            "job.finalize_conflict",
            job_id=str(job_id),
            target_status=JOB_STATUS_QUEUED,
            rowcount=res.rowcount,
        )
        return None
    logger.warning(
        "job.retried",
        job_id=str(job_id),
        job_type=job.job_type,
        attempts=job.attempts,
        next_run_at=next_run.isoformat(),
        correlation_id=job.correlation_id,
        error=truncated[:500],
    )
    return JOB_STATUS_QUEUED


async def recover_expired_leases(
    session: AsyncSession,
    *,
    now: datetime | None = None,
    job_type: str | None = None,
) -> int:
    """Recover `running` rows whose lease has expired.

    Two-phase to honor max_attempts:
    1. Rows with `attempts >= max_attempts` go straight to `dead`. Without
       this, a worker that crashes every time it tries to process a job
       would let the row recycle forever (claim → crash → recover → claim …)
       because `mark_failed` is the only other path to dead-letter and
       crashed workers never reach it.
    2. The rest go back to `queued` so a healthier worker can retry.

    Preserves `attempts` in both cases. Caller owns commit. Returns total
    rows touched (dead + re-queued)."""
    now = now or datetime.now(UTC)
    expired = [
        AsyncJob.status == JOB_STATUS_RUNNING,
        AsyncJob.lease_expires_at < now,
    ]
    if job_type is not None:
        expired.append(AsyncJob.job_type == job_type)

    dead_rows = (
        await session.execute(
            update(AsyncJob)
            .where(
                *expired,
                AsyncJob.attempts >= AsyncJob.max_attempts,
            )
            .values(
                status=JOB_STATUS_DEAD,
                lease_expires_at=None,
                last_error="lease_expired",
                updated_at=now,
            )
            .returning(
                AsyncJob.id,
                AsyncJob.job_type,
                AsyncJob.attempts,
                AsyncJob.max_attempts,
                AsyncJob.correlation_id,
            )
        )
    ).all()
    dead_count = len(dead_rows)
    if dead_count:
        logger.error("job.lease_recovered_to_dead", count=dead_count)
        for row in dead_rows:
            _alert_dead(
                job_id=row.id,
                job_type=row.job_type,
                attempts=row.attempts,
                max_attempts=row.max_attempts,
                correlation_id=row.correlation_id,
                error="lease_expired",
                cause="lease_expired",
            )

    requeue_res = cast(
        CursorResult[Any],
        await session.execute(
            update(AsyncJob)
            .where(*expired)
            .values(
                status=JOB_STATUS_QUEUED,
                lease_expires_at=None,
                last_error="lease_expired",
                updated_at=now,
                next_run_at=now,
            )
        ),
    )
    requeue_count = requeue_res.rowcount or 0
    if requeue_count:
        logger.warning("job.lease_recovered", count=requeue_count)

    return dead_count + requeue_count
