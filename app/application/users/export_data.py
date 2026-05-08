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

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.audit import record
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
    (V4 will surface a /v1/me/data-export/{job_id} status endpoint)."""
    job, inserted = await enqueue(
        session,
        job_type=EXPORT_JOB_TYPE,
        # One pending export per user — re-requesting while a prior is
        # in-flight returns the existing job. After completion the user
        # can request a new export (the next call's idempotency-key
        # construction includes a date stamp so today's export is
        # distinct from yesterday's).
        idempotency_key=f"user_export:{user.id}:pending",
        payload={"user_id": str(user.id)},
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
            "user.data_export_already_pending",
            user_id=str(user.id),
            job_id=str(job.id),
        )
    return job.id
