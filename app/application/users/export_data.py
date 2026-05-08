"""PIPL DSAR data-export — Phase E5 follow-up.

Plan §3.E left this implicit; spelled out for compliance: a user has the
right under PIPL to request a copy of all personal data we hold on them
before exercising deletion. Without this path, `aidrobe-private`'s 30-day
auto-purge would silently delete try-on body images the user might want
back.

Flow:
1. POST /v1/me/data-export → enqueue async-job(type=user_export)
2. Worker (skeleton stub for now — full bundle path lands when buckets
   are real) collects DB rows + object keys, packages a manifest, and
   surfaces a download link via push/email
3. Audit-log entry written so DSAR responses can show the request
   lifecycle even if the user later purges
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.audit import record
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.user import User
from app.infrastructure.workers.job_runtime import enqueue

logger = structlog.get_logger(__name__)

EXPORT_JOB_TYPE = "user_export"


async def request_data_export(
    session: AsyncSession,
    user: User,
) -> uuid.UUID:
    """Enqueue an export job and audit the request. Caller owns commit.
    Returns the job_id so the client can poll status if it wants
    (V4 will surface a /v1/me/data-export/{job_id} status endpoint).

    Concurrency: at most one in-flight export per user across days. A
    long-running export on day 1 still pending on day 2 must NOT spawn
    a second concurrent job — the first call into a new day returns
    the existing pending job until it terminates. After terminal status
    (succeeded/failed/dead) the per-day key opens up a fresh request."""
    # Look up any in-flight export for this user regardless of key date.
    in_flight = (
        await session.execute(
            select(AsyncJob)
            .where(
                AsyncJob.job_type == EXPORT_JOB_TYPE,
                AsyncJob.user_id == user.id,
                AsyncJob.status.in_(("queued", "running")),
            )
            .order_by(AsyncJob.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if in_flight is not None:
        logger.info(
            "user.data_export_already_in_flight",
            user_id=str(user.id),
            job_id=str(in_flight.id),
        )
        return in_flight.id

    today = datetime.now(UTC).date().isoformat()
    job, inserted = await enqueue(
        session,
        job_type=EXPORT_JOB_TYPE,
        # Day-keyed: today's request is idempotent within the day, but
        # tomorrow gets a fresh idempotency_key. Combined with the
        # in-flight short-circuit above, this gives "≤1 in-flight job
        # globally" + "fresh attempts after terminal status the next day".
        idempotency_key=f"user_export:{user.id}:{today}",
        payload={"user_id": str(user.id), "requested_for_date": today},
        user_id=user.id,
        target_id=user.id,
    )
    if inserted:
        await record(
            session,
            actor="user",
            action="user.data_export_requested",
            user_id=user.id,
            target_type="user",
            target_id=user.id,
        )
        logger.info("user.data_export_requested", user_id=str(user.id), job_id=str(job.id))
    else:
        logger.info(
            "user.data_export_already_today",
            user_id=str(user.id),
            job_id=str(job.id),
        )
    return job.id
