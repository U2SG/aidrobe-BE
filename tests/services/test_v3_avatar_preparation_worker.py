"""V3-013 — the preparation and purge workers, run for real.

`test_v3_avatar_lifecycle_apis.py` proves the request side only enqueues. This
file proves the other end actually lands: a queued preparation is claimed on the
worker's own session, the fake provider produces canonical bytes, the objects are
written to storage, a version row appears and the profile reaches `ready` — the
whole `preparing → ready` edge of contract §4.1, end to end.

Because `FakeAvatarPreparation` is local and deterministic (contract §6.4), this
is a genuine execution rather than a mock handshake: real bytes flow through the
port and into a storage double. What it does not prove — that a production model
produces a usable canonical avatar — is the budgeted, human-gated provider
evaluation, the same limit V3-012 recorded for its detector.

Harness follows `test_v3_avatar_candidate_worker.py`: a session factory bound to
the test's Postgres, patched over each worker module's `AsyncSessionLocal`, with
fixture rows committed through it (the workers run on a separate engine, so
uncommitted data would be invisible and the FK to `users` would fail).
"""

from __future__ import annotations

import io
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
import sqlalchemy as sa
from PIL import Image
from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.application.avatar.prepare_avatar import (
    JOB_TYPE as PREPARATION_JOB_TYPE,
)
from app.application.avatar.prepare_avatar import (
    MAX_ATTEMPTS,
    PreparationMode,
    PreparationStarted,
    PrepareAvatar,
)
from app.application.ports.avatar_preparation import PreparedAvatarAssets
from app.common.enums import (
    AvatarConsentStatus,
    AvatarQualityStatus,
    AvatarSourceType,
    AvatarStatus,
)
from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.avatar import (
    AvatarProfile,
    AvatarSourceAsset,
    AvatarVersion,
)
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.repositories.avatar_repo import AvatarLifecycle
from app.infrastructure.vision.fake_avatar_preparation import (
    CANONICAL_SIZE,
    MODEL_VERSION,
    PROVIDER,
)
from app.infrastructure.workers import avatar_preparation_worker, avatar_purge_worker
from app.infrastructure.workers.avatar_preparation_worker import (
    drain_avatar_preparation_jobs,
)
from app.infrastructure.workers.avatar_purge_worker import drain_avatar_purge_jobs
from app.infrastructure.workers.job_runtime import (
    JOB_STATUS_DEAD,
    JOB_STATUS_QUEUED,
)

CONSENT_COPY_V1 = "avatar_consent_placeholder_v1"


def _settings() -> Settings:
    return get_settings().model_copy(update={"ENABLE_V3_AVATAR": True})


def _photo_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (900, 1400), (180, 140, 100)).save(buffer, format="JPEG")
    return buffer.getvalue()


class _FakeStorage:
    """Records what was written and deleted, and serves the source photo."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.deleted: list[str] = []
        self.read_keys: list[str] = []

    async def get_object(self, key: str) -> bytes:
        self.read_keys.append(key)
        if key in self.objects:
            return self.objects[key]
        return _photo_bytes()

    async def put_object(self, key: str, data: bytes, content_type: str) -> str:
        self.objects[key] = data
        return f"https://storage.test/{key}"

    async def delete_objects(self, keys: Sequence[str]) -> None:
        self.deleted.extend(keys)


class _ExplodingPreparation:
    async def prepare(self, *, source_image: bytes) -> PreparedAvatarAssets:
        raise RuntimeError("provider is down")


class _FlakyPreparation:
    """Down until `heal()`, then works — one object, so a retry can succeed.

    The alternative (patch exploding, then `monkeypatch.undo()`) would also undo
    the storage double, and the retry would write to real storage.
    """

    def __init__(self) -> None:
        self.healthy = False
        self.calls = 0

    def heal(self) -> None:
        self.healthy = True

    async def prepare(self, *, source_image: bytes) -> PreparedAvatarAssets:
        self.calls += 1
        if not self.healthy:
            raise RuntimeError("provider is down")
        return PreparedAvatarAssets(
            front_image=b"front-bytes",
            side_image=None,
            provider="fake",
            model_version="recovered-1",
            prompt_version="v1",
        )


class _SideViewPreparation:
    async def prepare(self, *, source_image: bytes) -> PreparedAvatarAssets:
        return PreparedAvatarAssets(
            front_image=b"front-bytes",
            side_image=b"side-bytes",
            provider="fake",
            model_version="side-1",
            prompt_version="v1",
        )


@pytest_asyncio.fixture
async def factory(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(postgres_url, future=True)
    maker: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(avatar_preparation_worker, "AsyncSessionLocal", maker)
    monkeypatch.setattr(avatar_purge_worker, "AsyncSessionLocal", maker)
    try:
        yield maker
    finally:
        await engine.dispose()


@pytest.fixture
def storage(monkeypatch: pytest.MonkeyPatch) -> _FakeStorage:
    """Patched on each worker module — both import the name directly, so
    rebinding the container attribute would not be seen."""
    fake = _FakeStorage()
    monkeypatch.setattr(avatar_preparation_worker, "object_storage", lambda: fake)
    monkeypatch.setattr(avatar_purge_worker, "object_storage", lambda: fake)
    return fake


async def _seed_consented_user(
    maker: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID, str]:
    """A user with a `preparing` profile and one accepted source photo.

    Built through `offer_candidate` + `record_consent` — the transitions
    production uses — rather than by inserting the end state, because V3-011's
    CHECK constraints refuse several hand-written combinations and V3-012 lost a
    CI round to exactly that.
    """
    user_id = uuid.uuid4()
    async with maker() as session:
        session.add(User(id=user_id))
        await session.flush()
        profile = AvatarProfile(user_id=user_id, status=AvatarStatus.NONE.value)
        session.add(profile)
        object_key = f"users/{user_id}/avatar/source.jpg"
        asset = AvatarSourceAsset(
            user_id=user_id,
            source_type=AvatarSourceType.USER_UPLOAD.value,
            source_object_key=object_key,
            quality_status=AvatarQualityStatus.PASSED.value,
            consent_status=AvatarConsentStatus.PENDING.value,
        )
        session.add(asset)
        await session.flush()
        lifecycle = AvatarLifecycle(session)
        await lifecycle.offer_candidate(profile, asset)
        await lifecycle.record_consent(
            profile, asset, accept=True, consent_version=CONSENT_COPY_V1
        )
        profile_id = profile.id
        await session.commit()
    return user_id, profile_id, object_key


async def _queue_preparation(
    maker: async_sessionmaker[AsyncSession],
    user_id: uuid.UUID,
    mode: PreparationMode = PreparationMode.PREPARE,
) -> PreparationStarted:
    async with maker() as session:
        started = await PrepareAvatar(session, settings=_settings()).execute(
            user_id=user_id, mode=mode
        )
        await session.commit()
    return started


async def _job(maker: async_sessionmaker[AsyncSession], job_id: uuid.UUID) -> AsyncJob:
    async with maker() as session:
        row = await session.get(AsyncJob, job_id)
    assert row is not None
    return row


async def _preparation_job_count(maker: async_sessionmaker[AsyncSession]) -> int:
    """How many preparation rows exist at all.

    One row per (profile, photo, pipeline), *including across retries* — the
    revival re-opens the row rather than minting a second one, which is the
    property that keeps a photo's cost and error history in one place.
    """
    async with maker() as session:
        return len(
            list(
                (
                    await session.scalars(
                        sa.select(AsyncJob.id).where(AsyncJob.job_type == PREPARATION_JOB_TYPE)
                    )
                ).all()
            )
        )


async def _drain_until_dead(
    maker: async_sessionmaker[AsyncSession], job_id: uuid.UUID
) -> AsyncJob:
    """Drain until the job is out of attempts, fast-forwarding the backoff.

    `mark_failed` schedules the next attempt 60s out and doubles from there, so
    a test that only drained would see one failure and stop. Moving `next_run_at`
    into the past is the only thing simulated here: the attempts, the retries and
    the dead-letter are all real.
    """
    for _ in range(MAX_ATTEMPTS + 1):
        await drain_avatar_preparation_jobs(ctx=None)
        job = await _job(maker, job_id)
        if job.status == JOB_STATUS_DEAD:
            return job
        assert job.status == JOB_STATUS_QUEUED
        async with maker() as session:
            await session.execute(
                sa.update(AsyncJob)
                .where(AsyncJob.id == job_id)
                .values(next_run_at=datetime.now(UTC) - timedelta(seconds=1))
            )
            await session.commit()
    raise AssertionError("the preparation job never dead-lettered")


async def test_the_drain_prepares_a_canonical_version_and_marks_the_profile_ready(
    factory: async_sessionmaker[AsyncSession], storage: _FakeStorage
) -> None:
    """`preparing → ready`, end to end, with real bytes."""
    user_id, profile_id, source_key = await _seed_consented_user(factory)
    await _queue_preparation(factory, user_id)

    processed = await drain_avatar_preparation_jobs(ctx=None)
    assert processed == 1

    async with factory() as session:
        job = await session.scalar(
            sa.select(AsyncJob).where(AsyncJob.job_type == PREPARATION_JOB_TYPE)
        )
        assert job is not None
        assert job.status == "succeeded"

        profile = await session.get(AvatarProfile, profile_id)
        assert profile is not None
        assert profile.status == AvatarStatus.READY.value
        assert profile.active_version_id is not None

        version = await session.get(AvatarVersion, profile.active_version_id)
        assert version is not None
        assert version.avatar_profile_id == profile_id
        assert version.provider == PROVIDER
        assert version.model_version == MODEL_VERSION
        assert version.retired_at is None
        # Provenance: which photo this was built from (JSONB, not an FK, so a
        # retention purge of the photo cannot cascade the version away).
        assert version.source_asset_ids == [
            str(
                await session.scalar(
                    sa.select(AvatarSourceAsset.id).where(AvatarSourceAsset.user_id == user_id)
                )
            )
        ]

    # The source was read by its stable key, and the render was written under the
    # user's own prefix so `is_owned_object_key` recognizes it and the
    # account-level `delete_prefix` catch-all sweeps it.
    assert storage.read_keys == [source_key]
    assert len(storage.objects) == 1
    front_key = next(iter(storage.objects))
    assert front_key.startswith(f"users/{user_id}/")
    with Image.open(io.BytesIO(storage.objects[front_key])) as image:
        assert image.size == CANONICAL_SIZE


async def test_no_signed_url_or_image_byte_reaches_the_job_row(
    factory: async_sessionmaker[AsyncSession], storage: _FakeStorage
) -> None:
    """Contract §7, on the artifact that outlives log retention.

    `async_jobs` rows persist, so this is a stronger check than a log assertion:
    the payload and result must carry stable keys and provenance only.
    """
    user_id, profile_id, source_key = await _seed_consented_user(factory)
    await _queue_preparation(factory, user_id)
    await drain_avatar_preparation_jobs(ctx=None)

    async with factory() as session:
        job = await session.scalar(
            sa.select(AsyncJob).where(AsyncJob.job_type == PREPARATION_JOB_TYPE)
        )
        assert job is not None
        serialized = f"{job.payload}{job.result}{job.last_error}"

    assert "http" not in serialized
    assert "Signature" not in serialized
    assert "base64" not in serialized
    assert source_key in serialized


async def test_a_retryable_failure_leaves_the_profile_preparing(
    factory: async_sessionmaker[AsyncSession],
    storage: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A blip with attempts left is not a rollback.

    `mark_failed` puts the job back on the queue, so work really is still
    pending. Reporting `candidate` here would advertise material that is not on
    offer — the accepted asset is no longer *pending*, so the frozen GET shape
    renders `candidate: null` beside `status: candidate` — and the eventual
    success would then move `candidate → ready`, an edge §4.1 does not list.
    """
    monkeypatch.setattr(
        avatar_preparation_worker, "avatar_preparation", lambda: _ExplodingPreparation()
    )
    user_id, profile_id, _source_key = await _seed_consented_user(factory)
    started = await _queue_preparation(factory, user_id)

    await drain_avatar_preparation_jobs(ctx=None)

    job = await _job(factory, started.job_id)
    assert job.status == JOB_STATUS_QUEUED
    assert job.attempts == 1
    assert job.attempts < job.max_attempts
    async with factory() as session:
        profile = await session.get(AvatarProfile, profile_id)
        assert profile is not None
        assert profile.status == AvatarStatus.PREPARING.value


async def test_the_terminal_failure_returns_the_profile_to_candidate(
    factory: async_sessionmaker[AsyncSession],
    storage: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Contract §4.1's `preparing → candidate`, and no half-built version.

    Only once the attempts are exhausted: the user is left able to retry rather
    than stuck in `preparing`, and nothing was written that could later be
    activated.
    """
    monkeypatch.setattr(
        avatar_preparation_worker, "avatar_preparation", lambda: _ExplodingPreparation()
    )
    user_id, profile_id, _source_key = await _seed_consented_user(factory)
    started = await _queue_preparation(factory, user_id)

    job = await _drain_until_dead(factory, started.job_id)

    assert job.attempts == MAX_ATTEMPTS
    async with factory() as session:
        profile = await session.get(AvatarProfile, profile_id)
        assert profile is not None
        assert profile.status == AvatarStatus.CANDIDATE.value
        assert profile.active_version_id is None
        # The consent record survives the failure — our provider failing does not
        # change what the user agreed to.
        assert profile.consent_version == CONSENT_COPY_V1
        versions = list(
            (
                await session.scalars(
                    sa.select(AvatarVersion).where(AvatarVersion.avatar_profile_id == profile_id)
                )
            ).all()
        )
        assert versions == []


async def test_a_dead_preparation_is_retried_and_reaches_ready(
    factory: async_sessionmaker[AsyncSession],
    storage: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of `preparing → candidate`: a retry that actually runs.

    Every earlier failure test stopped at "the profile can retry" and none of
    them retried, which is how a dead end survived review. Before the fix this
    got as far as `prepare` and no further — the status guard refused a
    `candidate` profile, and past it the idempotency key handed back the dead
    row: HTTP 200, a `job_id`, and nothing behind it. The user's only recovery
    was a new photograph.

    Same photo, same consent, same key — the row is re-opened, and this asserts
    the retry lands rather than merely that it was accepted.
    """
    flaky = _FlakyPreparation()
    monkeypatch.setattr(avatar_preparation_worker, "avatar_preparation", lambda: flaky)
    user_id, profile_id, _source_key = await _seed_consented_user(factory)
    started = await _queue_preparation(factory, user_id)
    dead = await _drain_until_dead(factory, started.job_id)
    assert dead.status == JOB_STATUS_DEAD
    assert flaky.calls == MAX_ATTEMPTS

    # The user asks again from the state the failure left them in.
    retried = await _queue_preparation(factory, user_id)

    assert retried.created is True, "a retry that started work must not report a duplicate"
    assert retried.job_id == dead.id, "the revival re-opens the row, it does not mint a second"
    assert retried.profile_status == AvatarStatus.PREPARING.value
    revived = await _job(factory, dead.id)
    assert revived.status == JOB_STATUS_QUEUED
    assert revived.attempts == 0, "a user-initiated retry gets the full attempt budget back"
    assert revived.last_error == "RuntimeError", "why they are retrying stays on the row"
    assert await _preparation_job_count(factory) == 1

    flaky.heal()
    assert await drain_avatar_preparation_jobs(ctx=None) == 1

    async with factory() as session:
        profile = await session.get(AvatarProfile, profile_id)
        assert profile is not None
        assert profile.status == AvatarStatus.READY.value
        assert profile.active_version_id is not None
        version = await session.get(AvatarVersion, profile.active_version_id)
        assert version is not None
        assert version.model_version == "recovered-1"
    final = await _job(factory, dead.id)
    assert final.status == "succeeded"
    assert flaky.calls == MAX_ATTEMPTS + 1


async def test_a_dead_replacement_is_retried_without_disturbing_the_live_avatar(
    factory: async_sessionmaker[AsyncSession],
    storage: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The retry path runs for `replace` too, and must not open a gap.

    `replace` retries from `ready`, so this is where re-entering `preparing`
    would be actively wrong: the `active_version_matches_status` CHECK forces
    `active_version_id` NULL in `preparing`, i.e. the working avatar would
    disappear for the duration of a replacement that might fail again — the gap
    contract §4.2 forbids. `begin_preparation` moves `candidate` and nothing
    else, and this asserts the live version is still pointed at throughout.
    """
    flaky = _FlakyPreparation()
    user_id, profile_id, _source_key = await _seed_consented_user(factory)
    await _queue_preparation(factory, user_id)
    await drain_avatar_preparation_jobs(ctx=None)

    async with factory() as session:
        profile = await session.get(AvatarProfile, profile_id)
        assert profile is not None
        live_version_id = profile.active_version_id
        second = AvatarSourceAsset(
            user_id=user_id,
            source_type=AvatarSourceType.USER_UPLOAD.value,
            source_object_key=f"users/{user_id}/avatar/second.jpg",
            quality_status=AvatarQualityStatus.PASSED.value,
            consent_status=AvatarConsentStatus.PENDING.value,
        )
        session.add(second)
        await session.flush()
        await AvatarLifecycle(session).record_consent(
            profile, second, accept=True, consent_version=CONSENT_COPY_V1
        )
        await session.commit()

    monkeypatch.setattr(avatar_preparation_worker, "avatar_preparation", lambda: flaky)
    replacement = await _queue_preparation(factory, user_id, PreparationMode.REPLACE)
    dead = await _drain_until_dead(factory, replacement.job_id)
    assert dead.status == JOB_STATUS_DEAD

    async with factory() as session:
        profile = await session.get(AvatarProfile, profile_id)
        assert profile is not None
        # The failed replacement changed nothing: still ready, still the old
        # version. `fail_preparation` is a no-op outside `preparing`.
        assert profile.status == AvatarStatus.READY.value
        assert profile.active_version_id == live_version_id

    retried = await _queue_preparation(factory, user_id, PreparationMode.REPLACE)

    assert retried.created is True
    assert retried.job_id == replacement.job_id
    assert retried.profile_status == AvatarStatus.READY.value, (
        "a replacement retry must not take the working avatar away while it runs"
    )
    # Two rows total — the original preparation and the replacement — because a
    # different consented photo is a different key. The retry added neither.
    assert await _preparation_job_count(factory) == 2

    flaky.heal()
    assert await drain_avatar_preparation_jobs(ctx=None) == 1

    async with factory() as session:
        profile = await session.get(AvatarProfile, profile_id)
        assert profile is not None
        assert profile.status == AvatarStatus.READY.value
        assert profile.active_version_id != live_version_id
        retired = await session.get(AvatarVersion, live_version_id)
        assert retired is not None
        assert retired.retired_at is not None


async def test_a_succeeded_preparation_is_never_revived(
    factory: async_sessionmaker[AsyncSession], storage: _FakeStorage
) -> None:
    """Acceptance 2, at the exact point the retry path could have broken it.

    `_revive_if_terminal` is what makes a dead job re-runnable; if it treated
    `succeeded` as terminal too, every repeated `prepare` after a success would
    buy another provider run. The bound is the guarantee, so it is asserted
    directly rather than inferred from the dead-job tests.
    """
    user_id, profile_id, _source_key = await _seed_consented_user(factory)
    started = await _queue_preparation(factory, user_id)
    await drain_avatar_preparation_jobs(ctx=None)
    first = await _job(factory, started.job_id)
    assert first.status == "succeeded"

    async with factory() as session:
        repeat = await PrepareAvatar(session, settings=_settings()).execute(
            user_id=user_id, mode=PreparationMode.REPLACE
        )
        await session.commit()

    assert repeat.created is False
    assert repeat.job_id == first.id
    unchanged = await _job(factory, first.id)
    assert unchanged.status == "succeeded"
    assert unchanged.attempts == first.attempts
    assert await _preparation_job_count(factory) == 1
    # Nothing re-entered the queue, so nothing can be claimed and charged.
    assert await drain_avatar_preparation_jobs(ctx=None) == 0
    async with factory() as session:
        profile = await session.get(AvatarProfile, profile_id)
        assert profile is not None
        assert profile.status == AvatarStatus.READY.value


async def test_a_failure_persists_only_the_exception_type(
    factory: async_sessionmaker[AsyncSession],
    storage: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`async_jobs.error` must not carry the failing statement or its parameters.

    A DBAPI error embeds bound parameters — for this job type that includes the
    source object key. V3-012's round-3 review found the same exposure on the
    detection worker; this is the same rule enforced here rather than re-learned.
    """
    monkeypatch.setattr(
        avatar_preparation_worker, "avatar_preparation", lambda: _ExplodingPreparation()
    )
    user_id, profile_id, source_key = await _seed_consented_user(factory)
    await _queue_preparation(factory, user_id)

    await drain_avatar_preparation_jobs(ctx=None)

    async with factory() as session:
        job = await session.scalar(
            sa.select(AsyncJob).where(AsyncJob.job_type == PREPARATION_JOB_TYPE)
        )
        assert job is not None
        assert job.last_error == "RuntimeError"
        assert "provider is down" not in (job.last_error or "")
        assert source_key not in (job.last_error or "")


async def test_a_replacement_retires_the_previous_version_atomically(
    factory: async_sessionmaker[AsyncSession], storage: _FakeStorage
) -> None:
    """Contract §4.2's atomic swap: new version live, old one retired, no gap."""
    user_id, profile_id, _source_key = await _seed_consented_user(factory)
    await _queue_preparation(factory, user_id)
    await drain_avatar_preparation_jobs(ctx=None)

    async with factory() as session:
        profile = await session.get(AvatarProfile, profile_id)
        assert profile is not None
        first_version_id = profile.active_version_id
        # A second consented photo, which is what `replace` prepares from.
        second = AvatarSourceAsset(
            user_id=user_id,
            source_type=AvatarSourceType.USER_UPLOAD.value,
            source_object_key=f"users/{user_id}/avatar/second.jpg",
            quality_status=AvatarQualityStatus.PASSED.value,
            consent_status=AvatarConsentStatus.PENDING.value,
        )
        session.add(second)
        await session.flush()
        await AvatarLifecycle(session).record_consent(
            profile, second, accept=True, consent_version=CONSENT_COPY_V1
        )
        await session.commit()

    await _queue_preparation(factory, user_id, PreparationMode.REPLACE)
    await drain_avatar_preparation_jobs(ctx=None)

    async with factory() as session:
        profile = await session.get(AvatarProfile, profile_id)
        assert profile is not None
        assert profile.status == AvatarStatus.READY.value
        assert profile.active_version_id != first_version_id

        old = await session.get(AvatarVersion, first_version_id)
        assert old is not None
        assert old.retired_at is not None
        new = await session.get(AvatarVersion, profile.active_version_id)
        assert new is not None
        assert new.retired_at is None


async def test_a_side_render_is_stored_when_the_provider_returns_one(
    factory: async_sessionmaker[AsyncSession],
    storage: _FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The optional second view is wired, even though the P0 adapter returns none.

    Without this, `canonical_side_object_key` would be untested code that only
    V3-016's adapter exercises — and the branch that uploads it is exactly where a
    key collision between front and side would hide.
    """
    monkeypatch.setattr(
        avatar_preparation_worker, "avatar_preparation", lambda: _SideViewPreparation()
    )
    user_id, profile_id, _source_key = await _seed_consented_user(factory)
    await _queue_preparation(factory, user_id)

    await drain_avatar_preparation_jobs(ctx=None)

    async with factory() as session:
        profile = await session.get(AvatarProfile, profile_id)
        assert profile is not None
        version = await session.get(AvatarVersion, profile.active_version_id)
        assert version is not None
        assert version.canonical_side_object_key is not None
        assert version.canonical_side_object_key != version.canonical_front_object_key

    assert storage.objects[version.canonical_front_object_key] == b"front-bytes"
    assert storage.objects[version.canonical_side_object_key] == b"side-bytes"


async def test_a_deleted_profile_does_not_get_a_version_activated(
    factory: async_sessionmaker[AsyncSession], storage: _FakeStorage
) -> None:
    """A user's deletion may win a race against their own preparation.

    The job is claimed, the provider runs, and then the profile is gone. Nothing
    is resurrected: no version row, no error, and the job is terminal rather than
    retrying forever against a profile that will never come back.
    """
    user_id, profile_id, _source_key = await _seed_consented_user(factory)
    await _queue_preparation(factory, user_id)
    async with factory() as session:
        profile = await session.get(AvatarProfile, profile_id)
        assert profile is not None
        await session.delete(profile)
        await session.commit()

    await drain_avatar_preparation_jobs(ctx=None)

    async with factory() as session:
        job = await session.scalar(
            sa.select(AsyncJob).where(AsyncJob.job_type == PREPARATION_JOB_TYPE)
        )
        assert job is not None
        assert job.status == "succeeded"
        versions = list(
            (
                await session.scalars(
                    sa.select(AvatarVersion).where(AvatarVersion.avatar_profile_id == profile_id)
                )
            ).all()
        )
        assert versions == []


async def test_an_invalid_payload_fails_without_leaking_its_value(
    factory: async_sessionmaker[AsyncSession], storage: _FakeStorage
) -> None:
    """A malformed payload is permanently bad, and the error says only that."""
    async with factory() as session:
        session.add(
            AsyncJob(
                job_type=PREPARATION_JOB_TYPE,
                idempotency_key=f"{PREPARATION_JOB_TYPE}:broken:{uuid.uuid4()}",
                payload={"user_id": "not-a-uuid"},
                max_attempts=1,
            )
        )
        await session.commit()

    await drain_avatar_preparation_jobs(ctx=None)

    async with factory() as session:
        job = await session.scalar(
            sa.select(AsyncJob).where(AsyncJob.job_type == PREPARATION_JOB_TYPE)
        )
        assert job is not None
        assert job.last_error == "invalid_payload"
        assert "not-a-uuid" not in (job.last_error or "")


# --- the purge worker ------------------------------------------------------


async def test_the_purge_drain_deletes_the_objects_it_was_given(
    factory: async_sessionmaker[AsyncSession], storage: _FakeStorage
) -> None:
    """Acceptance 3's scheduled half, actually executing."""
    user_id = uuid.uuid4()
    keys = [f"users/{user_id}/avatar/source.jpg", f"users/{user_id}/avatar/canonical/front.png"]
    async with factory() as session:
        session.add(User(id=user_id))
        await session.flush()
        session.add(
            AsyncJob(
                job_type=avatar_purge_worker.JOB_TYPE,
                idempotency_key=f"{avatar_purge_worker.JOB_TYPE}:{uuid.uuid4()}",
                user_id=user_id,
                payload={"user_id": str(user_id), "object_keys": keys, "purge_derived": True},
            )
        )
        await session.commit()

    processed = await drain_avatar_purge_jobs(ctx=None)

    assert processed == 1
    assert storage.deleted == keys
    async with factory() as session:
        job = await session.scalar(
            sa.select(AsyncJob).where(AsyncJob.job_type == avatar_purge_worker.JOB_TYPE)
        )
        assert job is not None
        assert job.status == "succeeded"


async def test_a_purge_with_no_keys_succeeds_rather_than_paging_someone(
    factory: async_sessionmaker[AsyncSession], storage: _FakeStorage
) -> None:
    async with factory() as session:
        session.add(
            AsyncJob(
                job_type=avatar_purge_worker.JOB_TYPE,
                idempotency_key=f"{avatar_purge_worker.JOB_TYPE}:{uuid.uuid4()}",
                payload={"object_keys": []},
            )
        )
        await session.commit()

    await drain_avatar_purge_jobs(ctx=None)

    async with factory() as session:
        job = await session.scalar(
            sa.select(AsyncJob).where(AsyncJob.job_type == avatar_purge_worker.JOB_TYPE)
        )
        assert job is not None
        assert job.status == "succeeded"
    assert storage.deleted == []


async def test_a_storage_failure_persists_only_the_exception_type(
    factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The purge payload is a list of object keys, so its error must not quote one.

    Storage SDKs routinely include the failing key in the message, and
    `async_jobs.error` outlives log retention (contract §7).
    """
    leaking_key = "users/secret-user/avatar/source.jpg"

    class _ExplodingStorage:
        async def delete_objects(self, keys: Sequence[str]) -> None:
            raise RuntimeError(f"AccessDenied for {leaking_key}")

    monkeypatch.setattr(avatar_purge_worker, "object_storage", lambda: _ExplodingStorage())
    async with factory() as session:
        session.add(
            AsyncJob(
                job_type=avatar_purge_worker.JOB_TYPE,
                idempotency_key=f"{avatar_purge_worker.JOB_TYPE}:{uuid.uuid4()}",
                payload={"object_keys": [leaking_key]},
            )
        )
        await session.commit()

    await drain_avatar_purge_jobs(ctx=None)

    async with factory() as session:
        job = await session.scalar(
            sa.select(AsyncJob).where(AsyncJob.job_type == avatar_purge_worker.JOB_TYPE)
        )
        assert job is not None
        assert job.last_error == "RuntimeError"
        assert leaking_key not in (job.last_error or "")
