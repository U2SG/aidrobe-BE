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

**The resolved key must belong to the caller.** `image_url` arrives on a
free-form request field, and the worker reads whatever key comes out of it with
backend credentials — the URL's own access controls are no longer in the path.
`is_owned_object_key` is the same predicate V3-004's ingest uses, shared rather
than copied; only the response differs (a 404 there, a no-op here, because this
rides on a request whose real job is to save an OOTD photo).

**Known limit: per-purpose OSS buckets are not reachable, and say so.** A Phase
E5 upload through `POST /v1/uploads/sts-token` lands in `aidrobe-ootd` (etc.),
and `extract_internal_object_key` deliberately refuses those URLs while
`AliyunOssStorage.get_object` reads only `ALIYUN_OSS_BUCKET` — so such a photo
cannot be detected today. Today's client uses `/v1/uploads/presign` (the default
bucket), so source A does fire; the decision recorded here is that V3-012 covers
default-bucket uploads only, rather than reshaping the shared `ObjectStoragePort`
to carry a bucket — a change in code this repo cannot test, for a path no client
takes yet. What V3-012 does buy is that the gap is *loud*:
`UNRESOLVABLE_STORAGE_BUCKET` is a distinct outcome with its own warning, not the
same silence as a stranger's CDN URL. V3-013 owns `POST /v1/avatar-profile/uploads`
and settles the bucket layout for deliberate person photos.
"""

import hashlib
import uuid
from enum import StrEnum

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.avatar.detect_candidate import CandidateDetectionTrigger
from app.common.enums import AvatarSourceType
from app.config.settings import Settings
from app.infrastructure.storage.object_key import (
    extract_internal_object_key,
    is_owned_object_key,
    names_unresolvable_per_purpose_bucket,
)
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
#: most 122 characters, assessor included (a test pins that).
_DIGEST_CHARS = 40


class EnqueueOutcome(StrEnum):
    ENQUEUED = "enqueued"
    #: This photo already has a detection job for this trigger. The idempotent
    #: hit *is* prompt-once holding at the queue boundary.
    ALREADY_QUEUED = "already_queued"
    FEATURE_DISABLED = "feature_disabled"
    #: Not resolvable to one of our own storage keys — see the module docstring.
    NO_INTERNAL_OBJECT_KEY = "no_internal_object_key"
    #: Ours, but in a per-purpose bucket the read path cannot reach. Reported
    #: separately from the case above so this gap is observable rather than
    #: indistinguishable from an external URL.
    UNRESOLVABLE_STORAGE_BUCKET = "unresolvable_storage_bucket"
    #: The key resolved, but it was never issued to this caller. No job, and
    #: nothing said back to the client — the host flow is not this request's
    #: problem to fail.
    NOT_OWNED_OBJECT_KEY = "not_owned_object_key"
    #: The insert failed and was contained. The host flow is unaffected; the
    #: photo can be picked up by a later natural flow.
    ENQUEUE_FAILED = "enqueue_failed"


def assessor_identity(settings: Settings) -> str:
    """Which detector a job queued *now* would be answered by.

    Mirrors `container.person_frame_detector()` exactly — anything that is not
    an explicit `human_seg` resolves to the detector that determines nothing —
    and is bounded to those two words on purpose: it goes into an
    `idempotency_key` that must stay inside String(128), and
    `AVATAR_PERSON_DETECTOR` is free-form env text. A unit test asserts the two
    selectors stay in step, because a drift would silently mean the key claims
    an assessment the worker did not make.
    """
    return "human_seg" if settings.AVATAR_PERSON_DETECTOR == "human_seg" else "unavailable"


def candidate_detection_idempotency_key(
    user_id: uuid.UUID, object_key: str, trigger: CandidateDetectionTrigger, assessor: str
) -> str:
    """Stable per (user, photo, trigger, assessor).

    The trigger is part of the key because the two triggers ask different
    questions about the same photo. A natural flow that went quiet under
    `SkipReason.DECLINED_BY_USER` leaves no source-asset row behind, so without
    the trigger in the key a later user-initiated preview request would be
    silently swallowed as a duplicate of the job that deliberately did nothing —
    breaking the "unless the user later requests preview" half of acceptance 4.

    The **assessor** is part of the key for the same class of reason, found by
    review: with the default `AVATAR_PERSON_DETECTOR=unavailable` every photo is
    recorded `pending`, i.e. examined by nobody. If the key ignored which
    detector was configured, turning segmentation on later could never produce a
    second job for that photo, and the row would stay unassessed forever — the
    queue would be enforcing prompt-once over material nobody ever looked at.
    Versioning a key when the answer it caches can change is the same move as
    the `/today` payload cache bump to `p3`.

    Prompt-once does not weaken as a result: a *terminal* assessment still stops
    detection at the row (`DetectAvatarCandidate` skips it), so the second job
    finds the answer already recorded. The key is a cheap pre-filter; the row is
    the guarantee.
    """
    digest = hashlib.sha256(object_key.encode("utf-8")).hexdigest()[:_DIGEST_CHARS]
    return f"{JOB_TYPE}:{trigger.value}:{assessor}:{user_id}:{digest}"


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
        if names_unresolvable_per_purpose_bucket(image_url, settings):
            # Our own photo, in a bucket the read path cannot reach. Logged so
            # the day a client moves to the `ootd` bucket this shows up as a
            # signal instead of detection quietly never running. No URL and no
            # key in the line (contract §7).
            logger.warning(
                "avatar.candidate_detection.unresolvable_storage_bucket",
                user_id=str(user_id),
                source_type=source_type.value,
            )
            return EnqueueOutcome.UNRESOLVABLE_STORAGE_BUCKET
        return EnqueueOutcome.NO_INTERNAL_OBJECT_KEY

    if not is_owned_object_key(object_key, user_id):
        # A key this caller was never issued. The host flow still succeeds — it
        # is saving their own OOTD photo — but nothing about someone else's
        # object is read, queued or recorded under this user.
        logger.warning(
            "avatar.candidate_detection.foreign_object_key",
            user_id=str(user_id),
            source_type=source_type.value,
        )
        return EnqueueOutcome.NOT_OWNED_OBJECT_KEY

    try:
        async with session.begin_nested():
            _job, created = await enqueue(
                session,
                job_type=JOB_TYPE,
                idempotency_key=candidate_detection_idempotency_key(
                    user_id, object_key, trigger, assessor_identity(settings)
                ),
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
