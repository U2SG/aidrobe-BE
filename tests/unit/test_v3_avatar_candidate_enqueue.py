"""V3-012 — the enqueue key and the detector selector. No DB, no container.

Runs under `--noconftest`, so these are genuine local evidence. Everything here
is about the two boundaries where a wrong answer is silent: an idempotency key
that does not fit or is not stable (prompt-once leaks) and a detector selector
that picks the segmentation model when its optional deps are absent.
"""

import uuid

import pytest

from app.application.avatar.detect_candidate import CandidateDetectionTrigger
from app.application.avatar.enqueue_candidate_detection import (
    JOB_TYPE,
    MAX_ATTEMPTS,
    assessor_identity,
    candidate_detection_idempotency_key,
)
from app.config.settings import Settings
from app.infrastructure.persistence.models.async_job import AsyncJob

USER = uuid.UUID("11111111-2222-3333-4444-555555555555")
NATURAL = CandidateDetectionTrigger.NATURAL_FLOW
PREVIEW = CandidateDetectionTrigger.PREVIEW_REQUEST
NO_DETECTOR = "unavailable"
SEGMENTATION = "human_seg"


def _key(object_key: str, trigger: CandidateDetectionTrigger = NATURAL, *, assessor: str = NO_DETECTOR,
         user: uuid.UUID = USER) -> str:
    return candidate_detection_idempotency_key(user, object_key, trigger, assessor)


def _key_column_length() -> int:
    return int(AsyncJob.__table__.c.idempotency_key.type.length)


def test_the_key_fits_the_column_even_for_a_maximum_length_object_key() -> None:
    """`source_object_key` is String(512) and `idempotency_key` is String(128) —
    which is why the key is a digest rather than the path. A too-long key would
    fail the insert at runtime, inside a SAVEPOINT that swallows it, so the only
    symptom would be detection silently never happening."""
    longest = "users/" + "a" * 500
    key = _key(longest, PREVIEW, assessor=NO_DETECTOR)
    assert len(key) <= _key_column_length()
    # The worst case is the longer trigger name plus the longer assessor name;
    # pinned as a number so a future prefix change cannot quietly eat the
    # headroom (adding the assessor took it from 110 to 122 of 128).
    assert len(key) == 122
    assert len(_key(longest, PREVIEW, assessor=SEGMENTATION)) <= _key_column_length()


def test_the_key_is_stable_for_the_same_photo() -> None:
    assert _key("users/a/ootd/x.jpg") == _key("users/a/ootd/x.jpg")


def test_different_photos_get_different_keys() -> None:
    assert _key("users/a/ootd/x.jpg") != _key("users/a/ootd/y.jpg")


def test_different_users_get_different_keys() -> None:
    """Two users can legitimately hold the same object key path shape, and
    `one_row_per_source_object` is scoped per user — so the queue must be too."""
    other = uuid.UUID("99999999-2222-3333-4444-555555555555")
    assert _key("users/a/ootd/x.jpg") != _key("users/a/ootd/x.jpg", user=other)


def test_the_trigger_is_part_of_the_key() -> None:
    """A natural flow that went quiet under DECLINED_BY_USER leaves no source
    row behind. Without the trigger in the key, a later user-initiated preview
    for the same photo would be swallowed as a duplicate of the job that
    deliberately did nothing — breaking acceptance 4's "unless the user later
    requests preview" half."""
    assert _key("users/a/ootd/x.jpg") != _key("users/a/ootd/x.jpg", PREVIEW)


def test_the_key_is_prefixed_with_the_job_type() -> None:
    """Matches every other convention in docs/async-jobs.md, and keeps the
    admin observability views readable."""
    assert _key("users/a/x.jpg").startswith(f"{JOB_TYPE}:")


def test_the_assessor_is_part_of_the_key() -> None:
    """With no detector configured every photo is recorded `pending` — examined
    by nobody. If the key ignored which detector was configured, enabling
    segmentation later could never mint a second job for that photo, so the row
    would stay unassessed forever and the queue would be enforcing prompt-once
    over material nobody looked at."""
    assert _key("users/a/ootd/x.jpg", assessor=NO_DETECTOR) != _key(
        "users/a/ootd/x.jpg", assessor=SEGMENTATION
    )


def test_detection_is_not_retried() -> None:
    """A retry would re-segment the same bytes to the same verdict, and nothing
    user-visible is waiting. The documented consequence is that a transient
    storage miss is recovered by the next natural flow, not by the queue."""
    assert MAX_ATTEMPTS == 1


@pytest.mark.parametrize(
    ("configured", "expected"),
    [
        ("human_seg", SEGMENTATION),
        ("unavailable", NO_DETECTOR),
        ("", NO_DETECTOR),
        ("something-else", NO_DETECTOR),
        # Free-form env text must not reach the String(128) key.
        ("x" * 300, NO_DETECTOR),
    ],
)
def test_the_assessor_identity_is_bounded_to_two_words(configured: str, expected: str) -> None:
    assert assessor_identity(Settings(AVATAR_PERSON_DETECTOR=configured)) == expected


# --- the detector selector -------------------------------------------------


@pytest.mark.parametrize("configured", ["unavailable", "", "something-else"])
def test_the_default_detector_determines_nothing(
    monkeypatch: pytest.MonkeyPatch, configured: str
) -> None:
    """`rembg`/`onnxruntime` are not in the install set, so defaulting to the
    real model would turn a missing dependency into a stream of worker failures.
    Anything other than an explicit `human_seg` therefore resolves to the
    detector that determines nothing — material stays `pending`, never rejected."""
    from app.bootstrap import container
    from app.infrastructure.vision.person_frame_detector import UnavailablePersonFrameDetector

    monkeypatch.setattr(
        container, "get_settings", lambda: Settings(AVATAR_PERSON_DETECTOR=configured)
    )
    assert isinstance(container.person_frame_detector(), UnavailablePersonFrameDetector)


def test_local_segmentation_is_selected_only_when_asked_for(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.bootstrap import container
    from app.infrastructure.vision.person_frame_detector import MaskPersonFrameDetector

    monkeypatch.setattr(
        container, "get_settings", lambda: Settings(AVATAR_PERSON_DETECTOR="human_seg")
    )
    assert isinstance(container.person_frame_detector(), MaskPersonFrameDetector)


async def test_the_unavailable_detector_determines_nothing_rather_than_failing() -> None:
    """It must return a fully-undetermined observation, not raise: `pending`
    means "we did not look", and that is the honest state when no segmentation
    is configured."""
    from app.application.avatar.candidate_rules import assess_person_frame
    from app.common.enums import AvatarQualityStatus
    from app.infrastructure.vision.person_frame_detector import UnavailablePersonFrameDetector

    observation = await UnavailablePersonFrameDetector().observe(b"anything")
    assert observation.subject_region_count is None
    assert observation.sharpness_score is None
    assert assess_person_frame(observation).quality_status is AvatarQualityStatus.PENDING


@pytest.mark.parametrize("configured", ["human_seg", "unavailable", "", "something-else"])
def test_the_key_assessor_and_the_container_selector_stay_in_step(
    monkeypatch: pytest.MonkeyPatch, configured: str
) -> None:
    """Two places decide "which detector": the idempotency key (what a job
    claims it was assessed by) and the container (what actually assesses it).
    A drift between them would mean a key promising a verdict the worker never
    produces — a photo whose reassessment job is swallowed as a duplicate."""
    from app.bootstrap import container
    from app.infrastructure.vision.person_frame_detector import MaskPersonFrameDetector

    settings = Settings(AVATAR_PERSON_DETECTOR=configured)
    monkeypatch.setattr(container, "get_settings", lambda: settings)
    runs_segmentation = isinstance(container.person_frame_detector(), MaskPersonFrameDetector)
    assert (assessor_identity(settings) == SEGMENTATION) is runs_segmentation


# --- the two guards on the resolved key ------------------------------------


def test_a_key_under_the_callers_own_prefix_is_owned() -> None:
    """`/v1/uploads/presign` issues `users/<user_id>/…`, and `/v1/uploads/sts-token`
    scopes its credentials to `users/<user_id>/<action>/` — both are owned."""
    from app.infrastructure.storage.object_key import is_owned_object_key

    assert is_owned_object_key(f"users/{USER}/ootd/selfie.jpg", USER)
    assert is_owned_object_key(f"users/{USER}/upload/x.png", USER)


@pytest.mark.parametrize(
    "key",
    [
        "users/99999999-2222-3333-4444-555555555555/ootd/selfie.jpg",  # another user
        "items/shared/selfie.jpg",  # no user scope at all
        "users/ootd/selfie.jpg",  # missing the id segment
    ],
)
def test_a_key_the_caller_was_never_issued_is_not_owned(key: str) -> None:
    """The worker reads the resolved key with backend credentials, so the URL's
    own access controls are not in the path. Without this predicate a caller
    could name any key in the bucket and have someone else's photo persisted as
    their own avatar candidate — the same cross-user read `assert_owned_object_key`
    exists to stop on V3-004's ingest."""
    from app.infrastructure.storage.object_key import is_owned_object_key

    assert not is_owned_object_key(key, USER)


def test_a_traversal_segment_is_refused_even_under_the_right_prefix() -> None:
    from app.infrastructure.storage.object_key import is_owned_object_key

    assert not is_owned_object_key(f"users/{USER}/../{uuid.uuid4()}/ootd/x.jpg", USER)


def test_the_shared_predicate_is_the_one_v3_004_enforces() -> None:
    """One predicate, two responses (404 there, a silent no-job here). If these
    ever diverge, one surface gets a guard the other lost."""
    from app.application.purchase.ingest_candidate import assert_owned_object_key
    from app.common.exceptions import FriendlyException
    from app.infrastructure.storage.object_key import is_owned_object_key

    foreign = "users/99999999-2222-3333-4444-555555555555/uploads/x.jpg"
    assert not is_owned_object_key(foreign, USER)
    with pytest.raises(FriendlyException):
        assert_owned_object_key(foreign, USER)
    # And the owned case passes both.
    owned = f"users/{USER}/uploads/x.jpg"
    assert is_owned_object_key(owned, USER)
    assert_owned_object_key(owned, USER)


def _oss_settings() -> Settings:
    """A prod-shaped OSS config: a default bucket plus the Phase E5 per-purpose
    buckets, which is the layout in which the gap below exists."""
    return Settings(
        ALIYUN_OSS_ENDPOINT="https://oss-cn-hangzhou.aliyuncs.com",
        ALIYUN_OSS_BUCKET="aidrobe-default",
        ALIYUN_OSS_BUCKET_ITEMS="aidrobe-items",
        ALIYUN_OSS_BUCKET_OOTD="aidrobe-ootd",
        ALIYUN_OSS_BUCKET_PRIVATE="aidrobe-private",
        ALIYUN_OSS_BUCKET_DERIVED="aidrobe-derived",
    )


def test_an_ootd_bucket_url_is_recognised_as_ours_but_unreachable() -> None:
    """The recorded limitation, pinned so it cannot become a silent no-op.

    A Phase E5 upload through `/v1/uploads/sts-token` lands in `aidrobe-ootd`,
    and `extract_internal_object_key` deliberately refuses that URL (signing its
    key against the default bucket would produce a valid-looking URL for the
    wrong bucket). Detection therefore cannot reach the photo — but the enqueue
    must be able to *say so*, rather than reporting the same nothing it reports
    for a stranger's CDN URL."""
    from app.infrastructure.storage.object_key import (
        extract_internal_object_key,
        names_unresolvable_per_purpose_bucket,
    )

    settings = _oss_settings()
    for url in (
        f"https://aidrobe-ootd.oss-cn-hangzhou.aliyuncs.com/users/{USER}/ootd/x.jpg",
        f"https://oss-cn-hangzhou.aliyuncs.com/aidrobe-ootd/users/{USER}/ootd/x.jpg",
    ):
        assert extract_internal_object_key(url, settings) is None
        assert names_unresolvable_per_purpose_bucket(url, settings)


def test_the_default_bucket_and_foreign_urls_are_not_that_case() -> None:
    """The default bucket resolves normally (this is the path today's client
    takes, via `/v1/uploads/presign`), and an external URL must not be reported
    as our own unreachable object."""
    from app.infrastructure.storage.object_key import (
        extract_internal_object_key,
        names_unresolvable_per_purpose_bucket,
    )

    settings = _oss_settings()
    default_bucket_url = f"https://aidrobe-default.oss-cn-hangzhou.aliyuncs.com/users/{USER}/ootd/x.jpg"
    assert extract_internal_object_key(default_bucket_url, settings) == f"users/{USER}/ootd/x.jpg"
    assert not names_unresolvable_per_purpose_bucket(default_bucket_url, settings)
    assert not names_unresolvable_per_purpose_bucket("https://cdn.example.com/x.jpg", settings)
    # A single-bucket dev config that aliases a purpose onto the default bucket
    # is resolvable, so it is not this case either.
    dev = Settings(
        ALIYUN_OSS_ENDPOINT="https://oss-cn-hangzhou.aliyuncs.com",
        ALIYUN_OSS_BUCKET="aidrobe-default",
        ALIYUN_OSS_BUCKET_OOTD="aidrobe-default",
    )
    assert not names_unresolvable_per_purpose_bucket(default_bucket_url, dev)
