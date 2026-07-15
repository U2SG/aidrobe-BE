"""Asynchronous Today recompute worker for wardrobe mutations and fallback reads."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import structlog

from app.application.recommendations.background import (
    RECOMMENDATION_RECOMPUTE_JOB_TYPE,
    RecommendationBackgroundService,
)
from app.infrastructure.cache.client import (
    get_redis_client,
    invalidate_daily_recommendation_cache,
)
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)
from app.infrastructure.persistence.repositories.style_repo import SqlAlchemyStyleRepository
from app.infrastructure.persistence.session import AsyncSessionLocal
from app.infrastructure.workers.job_runtime import (
    claim_one,
    mark_failed,
    mark_succeeded,
)

logger = structlog.get_logger(__name__)
JOB_TYPE = RECOMMENDATION_RECOMPUTE_JOB_TYPE
MAX_DRAIN_PER_TICK = 5


async def _mark_payload_failure(job: AsyncJob, error: str) -> None:
    async with AsyncSessionLocal() as session:
        await mark_failed(
            session,
            job.id,
            claimed_attempt=job.attempts,
            error=error,
        )
        await session.commit()


async def _process_one(job: AsyncJob) -> None:
    payload: dict[str, Any] = job.payload or {}
    user_id_raw = payload.get("user_id")
    try:
        user_id = uuid.UUID(str(user_id_raw))
        requested_at = datetime.fromisoformat(str(payload.get("requested_at")))
        if requested_at.tzinfo is None:
            raise ValueError("requested_at must be timezone-aware")
    except (TypeError, ValueError):
        await _mark_payload_failure(job, "missing or invalid user_id/requested_at")
        return

    structlog.contextvars.bind_contextvars(
        correlation_id=job.correlation_id or "",
        job_id=str(job.id),
        user_id=str(user_id),
    )
    try:
        recomputed = False
        async with AsyncSessionLocal() as session:
            if await session.get(User, user_id) is not None:
                recomputed = await RecommendationBackgroundService(
                    SqlAlchemyRecommendationRepository(session),
                    SqlAlchemyStyleRepository(session),
                ).recompute_today(
                    user_id,
                    force=True,
                    requested_at=requested_at,
                )
            ok = await mark_succeeded(
                session,
                job.id,
                claimed_attempt=job.attempts,
                result={"recomputed": recomputed},
            )
            if not ok:
                await session.rollback()
                logger.warning(
                    "recommendation_recompute.lease_lost_discarding",
                    job_id=str(job.id),
                )
                return
            await session.commit()

        if recomputed:
            await invalidate_daily_recommendation_cache(get_redis_client(), user_id)
    except Exception as exc:
        logger.exception("recommendation_recompute.failed", error=str(exc))
        await _mark_payload_failure(job, str(exc))
    finally:
        structlog.contextvars.unbind_contextvars("correlation_id", "job_id", "user_id")


async def drain_recommendation_recompute_jobs(ctx: object) -> int:
    processed = 0
    for _ in range(MAX_DRAIN_PER_TICK):
        async with AsyncSessionLocal() as session:
            job = await claim_one(session, [JOB_TYPE])
        if job is None:
            break
        await _process_one(job)
        processed += 1
    if processed:
        logger.info("recommendation_recompute.drain_completed", processed=processed)
    return processed
