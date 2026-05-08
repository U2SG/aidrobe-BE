"""Phase E1 — soft-delete a user account, schedule hard purge.

Flow:
1. Stamp `users.deactivated_at = now()`. Auth blocks all subsequent requests.
2. Revoke all active sessions immediately so existing JWTs don't keep working
   until expiry.
3. Enqueue an `async_jobs` row of type=user_purge with `next_run_at` set to
   `deactivated_at + USER_PURGE_GRACE_DAYS` so the runtime fires the hard
   delete only after the grace window.
4. Audit-log the deactivation.

Idempotency: re-calling DELETE /v1/me on an already-deactivated user is a
no-op (returns success). The async_jobs idempotency_key prevents duplicate
purge jobs.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.audit import ACTION_USER_DEACTIVATE, record
from app.config.settings import get_settings
from app.infrastructure.persistence.models.user import User, UserSession
from app.infrastructure.workers.job_runtime import enqueue

logger = structlog.get_logger(__name__)

PURGE_JOB_TYPE = "user_purge"


def _purge_idempotency_key(user_id: uuid.UUID) -> str:
    return f"user_purge:{user_id}"


async def deactivate_account(
    session: AsyncSession,
    user: User,
) -> User:
    """Caller owns commit. Returns the updated User row."""
    settings = get_settings()
    if user.deactivated_at is not None:
        # Already deactivated — short-circuit. Important for clients that
        # retry on flaky networks; we MUST NOT re-enqueue the purge or
        # advance its next_run_at.
        logger.info("user.deactivate_idempotent_hit", user_id=str(user.id))
        return user

    now = datetime.now(UTC)
    user.deactivated_at = now

    # Revoke all live sessions: existing JWTs become useless on next request
    # via the auth gate in deps.py, but we also tear down the refresh side
    # so a refresh() call right now can't extend a session.
    await session.execute(
        update(UserSession)
        .where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
        .values(revoked_at=now)
    )

    # Schedule the hard purge for grace_days from now. The job_runtime drainer
    # already filters next_run_at <= now, so this row sits dormant until the
    # window closes. attempts/backoff/dead-letter all work for free.
    purge_at = now + timedelta(days=settings.USER_PURGE_GRACE_DAYS)
    job, inserted = await enqueue(
        session,
        job_type=PURGE_JOB_TYPE,
        idempotency_key=_purge_idempotency_key(user.id),
        payload={
            "user_id": str(user.id),
            "deactivated_at": now.isoformat(),
            "scheduled_purge_at": purge_at.isoformat(),
        },
        user_id=user.id,
        target_id=user.id,
    )
    if inserted:
        # Override next_run_at to the grace boundary. enqueue() defaults to
        # now() but we want this dormant until the window closes.
        job.next_run_at = purge_at
        await session.flush()

    await record(
        session,
        actor="user",
        action=ACTION_USER_DEACTIVATE,
        user_id=user.id,
        target_type="user",
        target_id=user.id,
        payload={
            "scheduled_purge_at": purge_at.isoformat(),
            "grace_days": settings.USER_PURGE_GRACE_DAYS,
        },
    )

    logger.warning(
        "user.deactivated",
        user_id=str(user.id),
        scheduled_purge_at=purge_at.isoformat(),
        grace_days=settings.USER_PURGE_GRACE_DAYS,
    )
    return user
