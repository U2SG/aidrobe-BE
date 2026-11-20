"""Album scan: compare garment crops across a batch's photos (job_type `garment_match`).

Scheduled by the batch read when cached verdicts don't cover every comparison.
One run loads the batch, answers what it can — local pre-screen first, then the
vision model within the run cap and the user's daily quota — caches the
verdicts on the batch, and schedules a follow-up if comparisons remain that a
new run could answer.
"""

from __future__ import annotations

import asyncio

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.cost_guard import resolve_quota_limit, usage_today
from app.application.import_tasks.batch_groups import GarmentImage
from app.application.import_tasks.import_batch import (
    MATCH_JOB_TYPE,
    MAX_CALLS_PER_RUN,
    CachedJudge,
    group_batch,
    load_batch_state,
    read_batch_view,
    save_verdicts,
)
from app.common.exceptions import FriendlyException
from app.config.settings import get_settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.session import AsyncSessionLocal
from app.infrastructure.storage.source_image import load_source_bytes
from app.infrastructure.vision.garment_similarity import prescreen
from app.infrastructure.workers.job_runtime import (
    claim_is_current,
    claim_one,
    mark_failed,
    mark_succeeded,
    recover_expired_leases,
    release_cancelled_claim,
)

logger = structlog.get_logger(__name__)

MATCH_ATTEMPT_TIMEOUT = 150
MATCH_LEASE_SECONDS = MATCH_ATTEMPT_TIMEOUT + 30
MATCH_MAX_DRAIN = 4


async def load_garment_image(image: GarmentImage) -> bytes:
    # Refs come from our own crop pipeline / wardrobe rows, so keys are trusted.
    return await load_source_bytes(image.url, image.key)


async def _match_one(session: AsyncSession, job: AsyncJob) -> None:
    from app.bootstrap.container import vision_service

    if job.target_id is None or job.user_id is None:
        raise ValueError("invalid garment match identity")
    user_id, batch_id = job.user_id, job.target_id
    try:
        state = await load_batch_state(session, user_id, batch_id)
    except FriendlyException:
        await mark_succeeded(session, job.id, claimed_attempt=job.attempts, result={"outcome": "obsolete"})
        await session.commit()
        return
    limit = resolve_quota_limit(MATCH_JOB_TYPE)
    remaining = None if limit is None else max(0, limit - await usage_today(session, user_id, MATCH_JOB_TYPE))
    budget = MAX_CALLS_PER_RUN if remaining is None else min(MAX_CALLS_PER_RUN, remaining)
    # Vision calls take seconds each; don't hold a pooled connection meanwhile.
    await session.commit()

    judge = CachedJudge(state.cache, vision_service(), load_garment_image, budget=budget, screen=prescreen)
    await group_batch(state, judge)
    quota_bound = remaining is not None and remaining <= MAX_CALLS_PER_RUN
    if judge.exhausted:
        outcome = "quota_limited" if quota_bound else "partial"
    else:
        outcome = "provider_error" if judge.errors else "complete"

    if not await claim_is_current(session, job):
        await session.rollback()
        return
    await save_verdicts(session, user_id, batch_id, judge.new)
    await mark_succeeded(
        session, job.id, claimed_attempt=job.attempts, cost_tokens=judge.calls,
        result={"outcome": outcome, "calls": judge.calls, "screened": judge.screened, "errors": judge.errors},
    )
    await session.commit()
    logger.info(
        "garment_match.done", batch_id=str(batch_id), outcome=outcome, calls=judge.calls, screened=judge.screened,
    )
    if outcome == "partial":
        # More comparisons than one run may make: queue the next run now rather
        # than waiting for the client's next poll.
        await read_batch_view(session, user_id, batch_id)
        await session.commit()


async def _process_match(job: AsyncJob) -> None:
    try:
        async with asyncio.timeout(MATCH_ATTEMPT_TIMEOUT), AsyncSessionLocal() as session:
            await _match_one(session, job)
    except asyncio.CancelledError:
        await release_cancelled_claim(job, AsyncSessionLocal)
        raise
    except Exception as exc:
        logger.warning(
            "garment_match.attempt_failed", job_id=str(job.id), batch_id=str(job.target_id),
            error_type=type(exc).__name__,
        )
        async with AsyncSessionLocal() as session:
            await mark_failed(session, job.id, claimed_attempt=job.attempts, error="garment_match_failed")
            await session.commit()


async def drain_garment_match_jobs(ctx: object) -> int:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + get_settings().ARQ_JOB_TIMEOUT_SECONDS - 5
    async with AsyncSessionLocal() as session:
        await recover_expired_leases(session, job_type=MATCH_JOB_TYPE)
        await session.commit()
    processed = 0
    while processed < MATCH_MAX_DRAIN and deadline - loop.time() >= MATCH_LEASE_SECONDS:
        async with AsyncSessionLocal() as session:
            job = await claim_one(session, [MATCH_JOB_TYPE], lease_seconds=MATCH_LEASE_SECONDS)
        if job is None:
            break
        await _process_match(job)
        processed += 1
    return processed


