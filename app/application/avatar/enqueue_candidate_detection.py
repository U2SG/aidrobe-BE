"""V3-012 — hand a photo to candidate detection without touching the flow it came from.

This is the whole of acceptance 2 ("a rejected candidate does not break OOTD or
import flows") on the *request* side. Two properties, both structural:

1. **The host request never runs detection.** All it does is insert one
   `async_jobs` row; segmentation, storage reads and the assessment happen later
   in `avatar_candidate_worker` on its own session. A rembg pass over a phone
   photo is measured in seconds, so doing it inline would not merely risk the
   OOTD response, it would visibly degrade it.
2. **Even that insert cannot fail the host request.** It runs inside a SAVEPOINT
   whose failure is swallowed and logged. That is not defensive padding: without
   it, an `IntegrityError` or a serialization failure on the *avatar* insert
   would mark the caller's whole transaction rollback-only, and the user's OOTD
   photo — the thing they actually asked to save — would be lost to a feature
   they have not even consented to yet. `DetectAvatarCandidate._record` uses the
   same SAVEPOINT shape for the same reason.

**The URL→key resolution lives here**, at the last point where a signed URL
legitimately exists. Downstream takes a stable `object_key`, so no signed URL
reaches `async_jobs.payload` (contract §7 forbids logging them, and a persisted
payload outlives a log line), and the idempotency key stays stable across
re-signings of the same photo. If it did not, one re-signed URL would mint a
second job for a photo the user already answered about — prompt-once leaking by
way of a query parameter.

A URL we cannot resolve to our own storage key is dropped without a job:
V3-011 acceptance 2 forbids persisting an expiring URL as durable truth, and an
external import URL is not ours to fetch person material from.
"""

import hashlib
import uuid
from enum import StrEnum

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.avatar.detect_candidate import CandidateDetectionTrigger
from app.common.enums import AvatarSourceType
from app.config.settings import Settings
from app.infrastructure.storage.object_key import extract_internal_object_key
from app.infrastructure.workers.job_runtime import enqueue

logger = structlog.get_logger(__name__)

JOB_TYPE = "avatar_candidate"

#: One attempt. A retry would re-run local segmentation over the same bytes and
#: reach the same verdict, and nothing user-visible is waiting on the result —
#: unlike `storage_purge`, where a lost job leaks objects. A genuinely transient
#: storage miss is recovered by the next natural flow instead, which is cheaper
#: than a lease sweep.
MAX_ATTEMPTS = 1

#: `async_jobs.idempotency_key` is String(128) and `source_object_key` is
#: String(512), so the key is hashed rather than embedded. 40 hex chars of
#: SHA-256 is the git-abbreviation convention and leaves the composed key at
#: most 110 characters (a test pins that).
_DIGEST_CHARS = 40


class EnqueueOutcome(StrEnum):
    ENQUEUED = "enqueued"
    #: This photo already has a detection job for this trigger. The idempotent
    #: hit *is* prompt-once holding at the queue boundary.
    ALREADY_QUEUED = "already_queued"
    FEATURE_DISABLED = "feature_disabled"
    #: Not resolvable to one of our own storage keys — see the module docstring.
    NO_INTERNAL_OBJECT_KEY = "no_internal_object_key"
    #: The insert failed and was contained. The host flow is unaffected; the
    #: photo can be picked up by a later natural flow.
    ENQUEUE_FAILED = "enqueue_failed"


def candidate_detection_idempotency_key(
    user_id: uuid.UUID, object_key: str, trigger: CandidateDetectionTrigger
) -> str:
    """Stable per (user, photo, trigger).

    The trigger is part of the key because the two triggers ask different
    questions about the same photo. A natural flow that went quiet under
    `SkipReason.DECLINED_BY_USER` leaves no source-asset row behind, so without
    the trigger in the key a later user-initiated preview request would be
    silently swallowed as a duplicate of the job that deliberately did nothing —
    breaking the "unless the user later requests preview" half of acceptance 4.
    """
    digest = hashlib.sha256(object_key.encode("utf-8")).hexdigest()[:_DIGEST_CHARS]
    return f"{JOB_TYPE}:{trigger.value}:{user_id}:{digest}"


async def enqueue_candidate_detection(
    session: AsyncSession,
    *,
    user_id: uuid.UUID,
    source_type: AvatarSourceType,
    image_url: str | None,
    settings: Settings,
    trigger: CandidateDetectionTrigger = CandidateDetectionTrigger.NATURAL_FLOW,
    correlation_id: str | None = None,
) -> EnqueueOutcome:
    """Queue detection for one photo. Never raises."""
    if not settings.ENABLE_V3_AVATAR:
        return EnqueueOutcome.FEATURE_DISABLED

    object_key = extract_internal_object_key(image_url, settings)
    if not object_key:
        return EnqueueOutcome.NO_INTERNAL_OBJECT_KEY

    try:
        async with session.begin_nested():
            _job, created = await enqueue(
                session,
                job_type=JOB_TYPE,
                idempotency_key=candidate_detection_idempotency_key(user_id, object_key, trigger),
                payload={
                    "user_id": str(user_id),
                    "source_type": source_type.value,
                    # The stable key, never a URL. See the module docstring.
                    "object_key": object_key,
                    "trigger": trigger.value,
                },
                user_id=user_id,
                correlation_id=correlation_id,
                max_attempts=MAX_ATTEMPTS,
            )
    except Exception:
        # No key, no URL, no user photo detail in the log line (contract §7).
        logger.warning("avatar.candidate_detection.enqueue_failed", user_id=str(user_id))
        return EnqueueOutcome.ENQUEUE_FAILED

    return EnqueueOutcome.ENQUEUED if created else EnqueueOutcome.ALREADY_QUEUED
