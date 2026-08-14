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
    candidate_detection_idempotency_key,
)
from app.config.settings import Settings
from app.infrastructure.persistence.models.async_job import AsyncJob

USER = uuid.UUID("11111111-2222-3333-4444-555555555555")
NATURAL = CandidateDetectionTrigger.NATURAL_FLOW
PREVIEW = CandidateDetectionTrigger.PREVIEW_REQUEST


def _key_column_length() -> int:
    return int(AsyncJob.__table__.c.idempotency_key.type.length)


def test_the_key_fits_the_column_even_for_a_maximum_length_object_key() -> None:
    """`source_object_key` is String(512) and `idempotency_key` is String(128) —
    which is why the key is a digest rather than the path. A too-long key would
    fail the insert at runtime, inside a SAVEPOINT that swallows it, so the only
    symptom would be detection silently never happening."""
    longest = "users/" + "a" * 500
    key = candidate_detection_idempotency_key(USER, longest, PREVIEW)
    assert len(key) <= _key_column_length()
    # The worst case is the longer trigger name; pinned as a number so a
    # future prefix change cannot quietly eat the headroom.
    assert len(key) == 110


def test_the_key_is_stable_for_the_same_photo() -> None:
    assert candidate_detection_idempotency_key(
        USER, "users/a/ootd/x.jpg", NATURAL
    ) == candidate_detection_idempotency_key(USER, "users/a/ootd/x.jpg", NATURAL)


def test_different_photos_get_different_keys() -> None:
    assert candidate_detection_idempotency_key(
        USER, "users/a/ootd/x.jpg", NATURAL
    ) != candidate_detection_idempotency_key(USER, "users/a/ootd/y.jpg", NATURAL)


def test_different_users_get_different_keys() -> None:
    """Two users can legitimately hold the same object key path shape, and
    `one_row_per_source_object` is scoped per user — so the queue must be too."""
    other = uuid.UUID("99999999-2222-3333-4444-555555555555")
    assert candidate_detection_idempotency_key(
        USER, "users/a/ootd/x.jpg", NATURAL
    ) != candidate_detection_idempotency_key(other, "users/a/ootd/x.jpg", NATURAL)


def test_the_trigger_is_part_of_the_key() -> None:
    """A natural flow that went quiet under DECLINED_BY_USER leaves no source
    row behind. Without the trigger in the key, a later user-initiated preview
    for the same photo would be swallowed as a duplicate of the job that
    deliberately did nothing — breaking acceptance 4's "unless the user later
    requests preview" half."""
    assert candidate_detection_idempotency_key(
        USER, "users/a/ootd/x.jpg", NATURAL
    ) != candidate_detection_idempotency_key(USER, "users/a/ootd/x.jpg", PREVIEW)


def test_the_key_is_prefixed_with_the_job_type() -> None:
    """Matches every other convention in docs/async-jobs.md, and keeps the
    admin observability views readable."""
    assert candidate_detection_idempotency_key(USER, "users/a/x.jpg", NATURAL).startswith(
        f"{JOB_TYPE}:"
    )


def test_detection_is_not_retried() -> None:
    """A retry would re-segment the same bytes to the same verdict, and nothing
    user-visible is waiting. The documented consequence is that a transient
    storage miss is recovered by the next natural flow, not by the queue."""
    assert MAX_ATTEMPTS == 1


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
