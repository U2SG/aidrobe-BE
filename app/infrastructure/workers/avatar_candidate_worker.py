"""V3-012 — run avatar candidate detection out of band.

Consumes `async_jobs` rows of type `avatar_candidate` enqueued by
`app.application.avatar.enqueue_candidate_detection`. This is the half of
acceptance 2 that the enqueue side cannot provide by itself: the assessment runs
here, on its own `AsyncSessionLocal` session, after the OOTD or import request
has already answered. There is no code path by which a detection outcome —
including a rejection — can reach the request that supplied the photo.

**No `reconcile_dead_*` sweep, deliberately.** `outfit_board` and `cutout` have
one because a dead job leaves a user-visible row stuck in a non-terminal status
that only a reconciler can resolve. A dead `avatar_candidate` job leaves nothing
visible at all: no profile transition happened, no asset row was promised to
anyone, and the photo stays eligible for a later natural flow. Adding a sweep
would be machinery guarding an invariant that cannot be violated.

**What CI does and does not exercise.** `rembg` / `onnxruntime` are optional and
absent in CI, so the container-selected detector there is
`UnavailablePersonFrameDetector` and every photo comes back `pending`. The
offer path is therefore driven by an injected fake detector in tests, exactly
as V3-005's pgvector-off environment limit was handled — the branch is covered,
the real segmentation model is not.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog

from app.application.avatar.detect_candidate import (
    CandidateDetectionTrigger,
    DetectAvatarCandidate,
)
from app.application.ports.avatar_detection import PersonFrameDetectorPort
from app.bootstrap.container import object_storage, person_frame_detector
from app.common.enums import AvatarSourceType
from app.config.settings import get_settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.session import AsyncSessionLocal
from app.infrastructure.workers.job_runtime import (
    claim_one,
    mark_failed,
    mark_succeeded,
)

logger = structlog.get_logger(__name__)

JOB_TYPE = "avatar_candidate"
MAX_DRAIN_PER_TICK = 5


async def _process_one(job: AsyncJob, *, detector: PersonFrameDetectorPort | None = None) -> None:
    payload: dict[str, Any] = job.payload or {}
    user_id_str = payload.get("user_id", "")
    object_key = payload.get("object_key") or ""
    source_type_value = payload.get("source_type", "")
    trigger_value = payload.get("trigger") or CandidateDetectionTrigger.NATURAL_FLOW.value

    structlog.contextvars.bind_contextvars(
        correlation_id=job.correlation_id or "",
        job_id=str(job.id),
        user_id=user_id_str,
    )
    try:
        try:
            user_id = uuid.UUID(user_id_str)
            source_type = AvatarSourceType(source_type_value)
            trigger = CandidateDetectionTrigger(trigger_value)
            if not object_key:
                raise ValueError("missing object_key")
        except (ValueError, KeyError):
            # A malformed payload is permanently bad; failing it is terminal
            # because MAX_ATTEMPTS is 1 (see the enqueue module). A fixed string
            # rather than the exception text: `AvatarSourceType("…")` and
            # `uuid.UUID("…")` both quote the offending value back, and
            # `async_jobs.error` is a persisted column, not a log line.
            async with AsyncSessionLocal() as session:
                await mark_failed(
                    session, job.id, claimed_attempt=job.attempts, error="invalid_payload"
                )
                await session.commit()
            return

        settings = get_settings()
        try:
            async with AsyncSessionLocal() as session:
                use_case = DetectAvatarCandidate(
                    session,
                    storage=object_storage(),
                    detector=detector if detector is not None else person_frame_detector(),
                    settings=settings,
                )
                result = await use_case.execute(
                    user_id=user_id,
                    source_type=source_type,
                    object_key=object_key,
                    trigger=trigger,
                )
                await session.commit()

            # `outcome` and `reason` are closed vocabularies over our own
            # decisions; no object key, URL or person detail is logged
            # (contract §7).
            logger.info(
                "avatar.candidate_detection.completed",
                outcome=result.outcome.value,
                reason=result.reason.value if result.reason else None,
                quality_status=result.quality_status,
                profile_status=result.profile_status,
                source_type=source_type.value,
            )

            async with AsyncSessionLocal() as session:
                ok = await mark_succeeded(session, job.id, claimed_attempt=job.attempts)
                if not ok:
                    await session.rollback()
                    logger.warning("avatar.candidate_detection.lease_lost_discarding", job_id=str(job.id))
                    return
                await session.commit()

        except Exception as exc:
            # Only the exception *type* is persisted. A DBAPI error carries the
            # failing statement and its bound parameters, which for this job
            # includes `source_object_key` — and `async_jobs.error` outlives any
            # log retention. The traceback still goes to the log/Sentry channel,
            # where operators need it; contract §7's persisted-artifact rules are
            # what this branch is protecting.
            logger.exception(
                "avatar.candidate_detection.failed", error_type=type(exc).__name__
            )
            async with AsyncSessionLocal() as session:
                await mark_failed(
                    session, job.id, claimed_attempt=job.attempts, error=type(exc).__name__
                )
                await session.commit()
    finally:
        structlog.contextvars.unbind_contextvars("correlation_id", "job_id", "user_id")


async def drain_avatar_candidate_jobs(ctx: object) -> int:
    processed = 0
    for _ in range(MAX_DRAIN_PER_TICK):
        async with AsyncSessionLocal() as session:
            job = await claim_one(session, [JOB_TYPE])
        if job is None:
            break
        await _process_one(job)
        processed += 1
    if processed:
        logger.info("avatar.candidate_detection.drain_completed", processed=processed)
    return processed
