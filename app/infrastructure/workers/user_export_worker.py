"""PIPL DSAR export worker — drains `user_export` async-jobs.

Collects the user's personal data (profile, settings, wardrobe items with
their storage object keys, wear history) into one JSON bundle, uploads it
to object storage under the user's own prefix, and stores the bundle's
`object_key` in the job result. The polling endpoint
(GET /v1/me/data-export/{job_id}) signs a short-lived download URL at read
time — we deliberately do NOT persist a signed URL (it would already be
expired when the user polls a day later; `object_key` is the stable
reference, per Phase E5 conventions).

Soft-deleted wardrobe items are included on purpose: DSAR completeness —
a row that still exists in the DB is still personal data we hold.

V4 extension point: grow _collect() with try-on assets / outfit history /
audit excerpts when those surfaces ship.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import inspect as sa_inspect
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.audit import record
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.recommendation import WearHistory
from app.infrastructure.persistence.models.user import User, UserSettings
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.session import AsyncSessionLocal
from app.infrastructure.workers.job_runtime import (
    claim_one,
    mark_failed,
    mark_succeeded,
)

logger = structlog.get_logger(__name__)

JOB_TYPE = "user_export"
# Exports are rare (≤1 in-flight per user by enqueue contract) and bundle
# an entire account — keep the tick light.
MAX_DRAIN_PER_TICK = 2

ACTION_USER_EXPORT_COMPLETED = "user.data_export_completed"
ACTION_USER_EXPORT_FAILED = "user.data_export_failed"

BUNDLE_FORMAT = "aidrobe.user_export.v1"


def _row_to_dict(obj: object) -> dict[str, Any]:
    mapper = sa_inspect(type(obj))
    assert mapper is not None  # all inputs are mapped ORM classes
    return {attr.key: getattr(obj, attr.key) for attr in mapper.column_attrs}


async def _collect(session: AsyncSession, user: User) -> dict[str, Any]:
    settings_row = await session.scalar(
        select(UserSettings).where(UserSettings.user_id == user.id)
    )
    items = (
        await session.scalars(
            select(WardrobeItem).where(WardrobeItem.user_id == user.id)
        )
    ).all()
    wears = (
        await session.scalars(
            select(WearHistory).where(WearHistory.user_id == user.id)
        )
    ).all()
    return {
        "format": BUNDLE_FORMAT,
        "generated_at": datetime.now(UTC).isoformat(),
        "user": _row_to_dict(user),
        "settings": _row_to_dict(settings_row) if settings_row else None,
        "wardrobe_items": [_row_to_dict(item) for item in items],
        "wear_history": [_row_to_dict(wear) for wear in wears],
    }


def _bundle_object_key(user_id: uuid.UUID, job_id: uuid.UUID) -> str:
    # Scoped under the user's own prefix so STS-scoped clients / lifecycle
    # rules treat it like the rest of their content.
    return f"users/{user_id}/export/{job_id}.json"


async def _process_one(job: AsyncJob) -> None:
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
        try:
            async with AsyncSessionLocal() as session:
                user = await session.get(User, user_id)
                if user is None:
                    # Purged before the export ran. Benign — the account (and
                    # anyone able to poll this job) is gone. Succeed-with-skip
                    # instead of dead-lettering so oncall isn't paged.
                    await mark_succeeded(
                        session,
                        job.id,
                        claimed_attempt=job.attempts,
                        result={"skipped": "user_purged"},
                    )
                    await session.commit()
                    logger.info("user_export.skipped_user_purged")
                    return
                bundle = await _collect(session, user)

            # Lazy import — same convention as outfit_board_worker; keeps the
            # worker importable without the storage container side effects.
            from app.bootstrap.container import object_storage

            data = json.dumps(bundle, ensure_ascii=False, default=str).encode("utf-8")
            object_key = _bundle_object_key(user_id, job.id)
            await object_storage().put_object(object_key, data, "application/json")

            async with AsyncSessionLocal() as session:
                ok = await mark_succeeded(
                    session,
                    job.id,
                    claimed_attempt=job.attempts,
                    result={"object_key": object_key, "bytes": len(data)},
                )
                if not ok:
                    await session.rollback()
                    logger.warning("user_export.lease_lost_discarding")
                    return
                await record(
                    session,
                    actor="system",
                    action=ACTION_USER_EXPORT_COMPLETED,
                    user_id=user_id,
                    target_type="user",
                    target_id=user_id,
                    payload={"object_key": object_key},
                )
                await session.commit()
            logger.info("user_export.completed", object_key=object_key, bytes=len(data))

        except Exception as exc:
            logger.exception("user_export.failed", error=str(exc))
            async with AsyncSessionLocal() as session:
                new_status = await mark_failed(
                    session, job.id, claimed_attempt=job.attempts, error=str(exc)
                )
                if new_status == "dead":
                    await record(
                        session,
                        actor="system",
                        action=ACTION_USER_EXPORT_FAILED,
                        user_id=user_id,
                        target_type="user",
                        target_id=user_id,
                        payload={"error": str(exc)[:500]},
                    )
                await session.commit()
            return
    finally:
        structlog.contextvars.unbind_contextvars("correlation_id", "job_id", "user_id")


async def drain_user_export_jobs(ctx: object) -> int:
    processed = 0
    for _ in range(MAX_DRAIN_PER_TICK):
        async with AsyncSessionLocal() as session:
            job = await claim_one(session, [JOB_TYPE])
        if job is None:
            break
        await _process_one(job)
        processed += 1
    if processed:
        logger.info("user_export.drain_completed", processed=processed)
    return processed
