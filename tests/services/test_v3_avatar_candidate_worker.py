"""V3-012 — the out-of-band half of acceptance 2, exercised for real.

`tests/api/test_v3_avatar_candidate_nonblocking.py` proves the host request only
enqueues. This file proves the other end actually runs: a queued photo is
claimed on the worker's OWN session, assessed, and the job reaches a terminal
state. Together they are the "detection cannot break the flow it rides on"
claim — one half would only be an assertion about intent.

Follows `test_ootd_poster_worker.py`'s harness: a session factory bound to the
test's Postgres, patched over the worker's `AsyncSessionLocal`, with rows
committed through it (the worker runs on a separate engine, so uncommitted
fixture data would be invisible to it — and the FK to `users` would fail).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.application.avatar.detect_candidate import (
    CandidateDetectionTrigger,
    DetectAvatarCandidate,
)
from app.application.avatar.enqueue_candidate_detection import (
    JOB_TYPE,
    enqueue_candidate_detection,
)
from app.application.ports.avatar_detection import PersonFrameObservation
from app.common.enums import (
    AvatarConsentStatus,
    AvatarQualityStatus,
    AvatarSourceType,
    AvatarStatus,
)
from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.avatar import AvatarProfile, AvatarSourceAsset
from app.infrastructure.persistence.models.user import User
from app.infrastructure.workers import avatar_candidate_worker
from app.infrastructure.workers.avatar_candidate_worker import (
    _process_one,
    drain_avatar_candidate_jobs,
)

STORAGE_BASE = Settings().LOCAL_STORAGE_BASE_URL.rstrip("/")


def _object_key(user_id: uuid.UUID) -> str:
    """User-scoped, because the enqueue refuses a key that was not issued to the
    caller. A fixed literal would make every drain assertion below test that
    guard instead of the worker."""
    return f"users/{user_id}/ootd/one.jpg"


def _internal_url(user_id: uuid.UUID) -> str:
    #: Derived from the configured storage base, not hard-coded — see the same
    #: note in tests/api/test_v3_avatar_candidate_nonblocking.py.
    return f"{STORAGE_BASE}/files/{_object_key(user_id)}"

GOOD_OBSERVATION = PersonFrameObservation(
    subject_region_count=1,
    subject_frame_fill_ratio=0.35,
    subject_vertical_span_ratio=0.9,
    top_edge_contact_ratio=0.0,
    sharpness_score=0.6,
    pixel_width=1080,
    pixel_height=1920,
    detector="fake_detector",
)


@pytest_asyncio.fixture
async def factory(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(postgres_url, future=True)
    maker: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(avatar_candidate_worker, "AsyncSessionLocal", maker)
    try:
        yield maker
    finally:
        await engine.dispose()


class _FakeStorage:
    async def get_object(self, key: str) -> bytes:
        return b"not-a-real-image"


class _FakeDetector:
    def __init__(self, observation: PersonFrameObservation) -> None:
        self.observation = observation

    async def observe(self, image_bytes: bytes) -> PersonFrameObservation:
        return self.observation


def _patch_ports(
    monkeypatch: pytest.MonkeyPatch, observation: PersonFrameObservation = GOOD_OBSERVATION
) -> None:
    """Patched on the worker module, not on the container: the worker imports
    both names directly, so rebinding the container attribute would not be seen."""
    monkeypatch.setattr(avatar_candidate_worker, "object_storage", lambda: _FakeStorage())
    monkeypatch.setattr(
        avatar_candidate_worker, "person_frame_detector", lambda: _FakeDetector(observation)
    )
    monkeypatch.setattr(
        avatar_candidate_worker,
        "get_settings",
        lambda: get_settings().model_copy(update={"ENABLE_V3_AVATAR": True}),
    )


async def _seed_user(maker: async_sessionmaker[AsyncSession]) -> uuid.UUID:
    user_id = uuid.uuid4()
    async with maker() as session:
        session.add(User(id=user_id))
        await session.commit()
    return user_id


async def _queue(
    maker: async_sessionmaker[AsyncSession], user_id: uuid.UUID, *, url: str | None = None
) -> None:
    async with maker() as session:
        await enqueue_candidate_detection(
            session,
            user_id=user_id,
            source_type=AvatarSourceType.OOTD_PHOTO,
            image_url=url if url is not None else _internal_url(user_id),
            settings=get_settings().model_copy(update={"ENABLE_V3_AVATAR": True}),
        )
        await session.commit()


async def test_the_drain_assesses_a_queued_photo_and_marks_the_job_succeeded(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_ports(monkeypatch)
    user_id = await _seed_user(factory)
    await _queue(factory, user_id)

    processed = await drain_avatar_candidate_jobs(ctx=None)
    assert processed == 1

    async with factory() as session:
        job = await session.scalar(sa.select(AsyncJob).where(AsyncJob.job_type == JOB_TYPE))
        assert job is not None
        assert job.status == "succeeded"

        asset = await session.scalar(
            sa.select(AvatarSourceAsset).where(AvatarSourceAsset.user_id == user_id)
        )
        assert asset is not None
        assert asset.source_object_key == _object_key(user_id)
        assert asset.quality_status == AvatarQualityStatus.PASSED.value
        # Acceptance 1 across the whole out-of-band path, not only in the
        # use case: the worker cannot grant consent either.
        assert asset.consent_status == AvatarConsentStatus.PENDING.value

        profile = await session.scalar(
            sa.select(AvatarProfile).where(AvatarProfile.user_id == user_id)
        )
        assert profile is not None
        assert profile.status == AvatarStatus.CANDIDATE.value
        assert profile.consented_at is None


async def test_an_empty_queue_drains_to_zero(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_ports(monkeypatch)
    assert await drain_avatar_candidate_jobs(ctx=None) == 0


async def test_a_malformed_payload_fails_its_job_instead_of_killing_the_drain(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bad payload is permanently bad, and `max_attempts=1` makes the failure
    terminal. What matters is that the drain reports it and keeps going."""
    _patch_ports(monkeypatch)
    async with factory() as session:
        session.add(
            AsyncJob(
                job_type=JOB_TYPE,
                idempotency_key=f"{JOB_TYPE}:broken:{uuid.uuid4()}",
                payload={"user_id": "not-a-uuid", "object_key": _object_key(uuid.uuid4())},
                max_attempts=1,
            )
        )
        await session.commit()

    assert await drain_avatar_candidate_jobs(ctx=None) == 1
    async with factory() as session:
        job = await session.scalar(sa.select(AsyncJob).where(AsyncJob.job_type == JOB_TYPE))
        assert job is not None
        assert job.status in {"failed", "dead"}
        assert (await session.scalars(sa.select(AvatarSourceAsset))).all() == []


async def test_a_missing_object_key_fails_the_job_rather_than_assessing_nothing(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    _patch_ports(monkeypatch)
    user_id = await _seed_user(factory)
    async with factory() as session:
        session.add(
            AsyncJob(
                job_type=JOB_TYPE,
                idempotency_key=f"{JOB_TYPE}:nokey:{uuid.uuid4()}",
                payload={"user_id": str(user_id), "source_type": "ootd_photo"},
                max_attempts=1,
            )
        )
        await session.commit()

    assert await drain_avatar_candidate_jobs(ctx=None) == 1
    async with factory() as session:
        job = await session.scalar(sa.select(AsyncJob).where(AsyncJob.job_type == JOB_TYPE))
        assert job is not None
        assert job.status in {"failed", "dead"}


async def test_a_rejected_photo_still_completes_its_job(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Acceptance 2 at the queue level: a *rejection* is a normal successful
    detection run, not a job failure. If it failed the job, a crowd photo would
    look like an infrastructure problem forever."""
    two_people = PersonFrameObservation(
        subject_region_count=3,
        subject_frame_fill_ratio=0.4,
        subject_vertical_span_ratio=0.9,
        top_edge_contact_ratio=0.0,
        sharpness_score=0.6,
        pixel_width=1080,
        pixel_height=1920,
        detector="fake_detector",
    )
    _patch_ports(monkeypatch, two_people)
    user_id = await _seed_user(factory)
    await _queue(factory, user_id)

    assert await drain_avatar_candidate_jobs(ctx=None) == 1
    async with factory() as session:
        job = await session.scalar(sa.select(AsyncJob).where(AsyncJob.job_type == JOB_TYPE))
        assert job is not None
        assert job.status == "succeeded"
        asset = await session.scalar(
            sa.select(AvatarSourceAsset).where(AvatarSourceAsset.user_id == user_id)
        )
        assert asset is not None
        assert asset.quality_status == AvatarQualityStatus.REJECTED.value
        assert (
            await session.scalar(sa.select(AvatarProfile).where(AvatarProfile.user_id == user_id))
        ) is None


async def test_the_savepoint_contains_a_duplicate_on_a_worker_owned_session(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_record` wraps its insert in `session.begin_nested()`. The precedent it
    follows (V3-007) ran inside a request-scoped session that already had an
    open transaction; the worker's session is created fresh with none, so
    autobegin has to produce a usable SAVEPOINT. Verified rather than assumed —
    if it did not, every concurrent duplicate would poison the worker session
    and fail a job that should simply have skipped.
    """
    _patch_ports(monkeypatch)
    user_id = await _seed_user(factory)

    async with factory() as session:
        session.add(
            AvatarSourceAsset(
                user_id=user_id,
                source_type=AvatarSourceType.OOTD_PHOTO.value,
                source_object_key=_object_key(user_id),
                quality_status=AvatarQualityStatus.PASSED.value,
            )
        )
        await session.commit()

    async with factory() as session:
        use_case = DetectAvatarCandidate(
            session,
            storage=_FakeStorage(),  # type: ignore[arg-type]
            detector=_FakeDetector(GOOD_OBSERVATION),  # type: ignore[arg-type]
            settings=get_settings().model_copy(update={"ENABLE_V3_AVATAR": True}),
        )
        duplicate = await use_case._record(
            user_id=user_id,
            source_type=AvatarSourceType.OOTD_PHOTO,
            object_key=_object_key(user_id),
            quality_status=AvatarQualityStatus.PASSED,
            signals={"signals_version": "v3-012.1"},
        )
        assert duplicate is None
        # The session survived the contained 23505 and can still be used.
        assert await session.scalar(sa.select(sa.literal(1))) == 1
        assert (
            len((await session.scalars(sa.select(AvatarSourceAsset))).all()) == 1
        )


async def test_a_declined_user_is_left_alone_by_the_worker_too(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The cross-asset §7.3 rule, end to end through the queue rather than only
    at the use-case boundary: the job completes successfully having done nothing.
    """
    _patch_ports(monkeypatch)
    user_id = await _seed_user(factory)
    async with factory() as session:
        session.add(
            AvatarSourceAsset(
                user_id=user_id,
                source_type=AvatarSourceType.OOTD_PHOTO.value,
                source_object_key=f"users/{user_id}/ootd/declined.jpg",
                quality_status=AvatarQualityStatus.PASSED.value,
                consent_status=AvatarConsentStatus.DECLINED.value,
                consent_decided_at=sa.func.now(),
            )
        )
        await session.commit()

    await _queue(factory, user_id)
    assert await drain_avatar_candidate_jobs(ctx=None) == 1

    async with factory() as session:
        job = await session.scalar(sa.select(AsyncJob).where(AsyncJob.job_type == JOB_TYPE))
        assert job is not None
        assert job.status == "succeeded"
        assets = list((await session.scalars(sa.select(AvatarSourceAsset))).all())
        assert len(assets) == 1  # the declined one, nothing new
        assert (
            await session.scalar(sa.select(AvatarProfile).where(AvatarProfile.user_id == user_id))
        ) is None


def test_the_worker_declares_no_dead_letter_reconciler() -> None:
    """Deliberate, and pinned so an evaluator's "why does this queue have no
    reconcile_dead_*?" has an answer in the code. `outfit_board` needs one
    because a dead job strands a user-visible row; a dead detection job strands
    nothing, and the photo remains eligible for a later natural flow."""
    assert not [name for name in dir(avatar_candidate_worker) if name.startswith("reconcile_")]
    assert avatar_candidate_worker.MAX_DRAIN_PER_TICK > 0


async def test_the_trigger_survives_the_queue(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A preview-request job must not silently degrade to a natural flow when it
    crosses the queue — that would re-suppress exactly the case acceptance 4
    carves out."""
    _patch_ports(monkeypatch)
    user_id = await _seed_user(factory)
    async with factory() as session:
        await enqueue_candidate_detection(
            session,
            user_id=user_id,
            source_type=AvatarSourceType.USER_UPLOAD,
            image_url=_internal_url(user_id),
            settings=get_settings().model_copy(update={"ENABLE_V3_AVATAR": True}),
            trigger=CandidateDetectionTrigger.PREVIEW_REQUEST,
        )
        await session.commit()

    async with factory() as session:
        job = await session.scalar(sa.select(AsyncJob).where(AsyncJob.job_type == JOB_TYPE))
        assert job is not None
        assert (job.payload or {})["trigger"] == CandidateDetectionTrigger.PREVIEW_REQUEST.value
        assert CandidateDetectionTrigger.PREVIEW_REQUEST.value in job.idempotency_key


async def test_process_one_is_reachable_with_an_injected_detector(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """`_process_one`'s `detector` seam exists so the offer path is testable
    where rembg is absent (CI). Exercised here so it cannot rot."""
    monkeypatch.setattr(avatar_candidate_worker, "object_storage", lambda: _FakeStorage())
    monkeypatch.setattr(
        avatar_candidate_worker,
        "get_settings",
        lambda: get_settings().model_copy(update={"ENABLE_V3_AVATAR": True}),
    )
    user_id = await _seed_user(factory)
    await _queue(factory, user_id)

    from app.infrastructure.workers.job_runtime import claim_one

    async with factory() as session:
        job = await claim_one(session, [JOB_TYPE])
    assert job is not None

    await _process_one(job, detector=_FakeDetector(GOOD_OBSERVATION))  # type: ignore[arg-type]

    async with factory() as session:
        asset = await session.scalar(
            sa.select(AvatarSourceAsset).where(AvatarSourceAsset.user_id == user_id)
        )
        assert asset is not None
        assert asset.quality_status == AvatarQualityStatus.PASSED.value
