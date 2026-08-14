"""V3-013 — consent, preparation idempotency and deletion against real Postgres.

The three `evidence_required` items live here at the level where they mean
something: consent recorded with its copy version and timestamp, a second
`prepare` that buys nothing, and a delete that removes derived assets and
schedules their storage removal.

DB-backed because the guarantees are database guarantees. The CHECK constraints
refuse a consent with no timestamp; `enqueue`'s unique index is what makes a
duplicate collide; and `delete_avatar` has to disable before deleting versions or
the `active_version_matches_status` CHECK rejects the implicit SET NULL. A unit
test with a fake session would assert the code's opinion of all three.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.avatar.delete_avatar import JOB_TYPE as PURGE_JOB_TYPE
from app.application.avatar.delete_avatar import (
    AvatarProfileNotFound,
    DeleteAvatarProfile,
)
from app.application.avatar.prepare_avatar import (
    JOB_TYPE as PREPARATION_JOB_TYPE,
)
from app.application.avatar.prepare_avatar import (
    PreparationMode,
    PreparationRefusal,
    PreparationRefused,
    PrepareAvatar,
)
from app.common.enums import (
    AvatarConsentStatus,
    AvatarQualityStatus,
    AvatarSourceType,
    AvatarStatus,
)
from app.config.settings import Settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.avatar import (
    AvatarProfile,
    AvatarSourceAsset,
    AvatarVersion,
)
from app.infrastructure.persistence.models.outfit_visual import OutfitVisualAsset
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.repositories.avatar_repo import AvatarLifecycle
from app.infrastructure.workers.job_runtime import (
    JOB_STATUS_DEAD,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
)

CONSENT_COPY_V1 = "avatar_consent_placeholder_v1"


def _settings() -> Settings:
    return Settings(APP_ENV="dev", ENABLE_V3_AVATAR=True)


async def _seed_user(session: AsyncSession) -> uuid.UUID:
    user_id = uuid.uuid4()
    session.add(User(id=user_id))
    await session.flush()
    return user_id


async def _seed_candidate(
    session: AsyncSession,
    *,
    source_type: AvatarSourceType = AvatarSourceType.CAMERA_CAPTURE,
) -> tuple[uuid.UUID, AvatarProfile, AvatarSourceAsset]:
    """A user whose profile is `candidate` with one qualified, undecided photo.

    Built through `offer_candidate` rather than by inserting the end state, so the
    fixture goes through the same transition production does — the V3-012 round-2
    lesson, where profiles seeded directly in states the CHECK constraints forbid
    made three tests fail for reasons unrelated to what they asserted.
    """
    user_id = await _seed_user(session)
    profile = AvatarProfile(user_id=user_id, status=AvatarStatus.NONE.value)
    session.add(profile)
    asset = AvatarSourceAsset(
        user_id=user_id,
        source_type=source_type.value,
        source_object_key=f"users/{user_id}/avatar/source.jpg",
        quality_status=AvatarQualityStatus.PASSED.value,
        consent_status=AvatarConsentStatus.PENDING.value,
    )
    session.add(asset)
    await session.flush()
    await AvatarLifecycle(session).offer_candidate(profile, asset)
    return user_id, profile, asset


async def _jobs(session: AsyncSession, job_type: str) -> list[AsyncJob]:
    return list(
        (await session.scalars(sa.select(AsyncJob).where(AsyncJob.job_type == job_type))).all()
    )


# --- evidence 1: consent API ----------------------------------------------


async def test_accepting_records_the_copy_version_and_the_timestamp(
    db_session: AsyncSession,
) -> None:
    """Acceptance 1, on both rows.

    The asset keeps which version *this photo* was accepted under; the profile
    keeps what the frozen GET shape returns as `consent`, which must stay
    answerable after `status=ready` leaves `candidate` null and after the photo is
    purged at retention.
    """
    user_id, profile, asset = await _seed_candidate(db_session)
    before = datetime.now(UTC)

    await AvatarLifecycle(db_session).record_consent(
        profile, asset, accept=True, consent_version=CONSENT_COPY_V1
    )

    assert asset.consent_status == AvatarConsentStatus.ACCEPTED.value
    assert asset.consent_version == CONSENT_COPY_V1
    assert asset.consent_decided_at is not None
    assert asset.consent_decided_at >= before
    assert profile.consent_version == CONSENT_COPY_V1
    assert profile.consented_at == asset.consent_decided_at
    # `candidate → preparing` on explicit consent (contract §4.1).
    assert profile.status == AvatarStatus.PREPARING.value


async def test_declining_records_a_decision_but_no_consent_version(
    db_session: AsyncSession,
) -> None:
    """A declined photo must not carry a version — that would imply agreement."""
    user_id, profile, asset = await _seed_candidate(db_session)

    await AvatarLifecycle(db_session).record_consent(profile, asset, accept=False)

    assert asset.consent_status == AvatarConsentStatus.DECLINED.value
    assert asset.consent_version is None
    assert asset.consent_decided_at is not None
    # Refusing one photo is not revoking the avatar: no tombstone, no consent.
    assert profile.status == AvatarStatus.NONE.value
    assert profile.disabled_at is None
    assert profile.consent_version is None


async def test_a_decided_consent_cannot_be_flipped(db_session: AsyncSession) -> None:
    """Terminal means the row cannot be re-decided.

    Otherwise a replayed request could flip a decline to an acceptance, or
    re-accept under newer copy and silently rewrite the audit record of what the
    user was actually shown.
    """
    user_id, profile, asset = await _seed_candidate(db_session)
    lifecycle = AvatarLifecycle(db_session)
    await lifecycle.record_consent(profile, asset, accept=False)

    with pytest.raises(ValueError):
        await lifecycle.record_consent(
            profile, asset, accept=True, consent_version=CONSENT_COPY_V1
        )

    assert asset.consent_status == AvatarConsentStatus.DECLINED.value


async def test_an_exact_repeat_is_an_idempotent_retry(db_session: AsyncSession) -> None:
    """Network retries are normal and must not 500 or double-write.

    The timestamp is asserted unchanged: a retry that refreshed
    `consent_decided_at` would move the audit record of *when* the user agreed.
    """
    user_id, profile, asset = await _seed_candidate(db_session)
    lifecycle = AvatarLifecycle(db_session)
    await lifecycle.record_consent(
        profile, asset, accept=True, consent_version=CONSENT_COPY_V1
    )
    first_decided_at = asset.consent_decided_at

    await lifecycle.record_consent(
        profile, asset, accept=True, consent_version=CONSENT_COPY_V1
    )

    assert asset.consent_decided_at == first_decided_at


async def test_the_database_refuses_a_decided_consent_with_no_timestamp(
    db_session: AsyncSession,
) -> None:
    """Acceptance 1 is enforced by the schema, not only by the code path.

    `decided_consent_has_timestamp` is what makes "consent version and timestamp
    are recorded" true for rows written by anything — a migration, a fixture, a
    future endpoint — rather than only by `record_consent`.
    """
    user_id = await _seed_user(db_session)
    db_session.add(
        AvatarSourceAsset(
            user_id=user_id,
            source_type=AvatarSourceType.CAMERA_CAPTURE.value,
            source_object_key=f"users/{user_id}/avatar/no-timestamp.jpg",
            quality_status=AvatarQualityStatus.PASSED.value,
            consent_status=AvatarConsentStatus.ACCEPTED.value,
            consent_version=CONSENT_COPY_V1,
            consent_decided_at=None,
        )
    )

    with pytest.raises(sa.exc.IntegrityError):
        await db_session.flush()
    await db_session.rollback()


# --- evidence 2: idempotency ----------------------------------------------


async def _consent_and_prepare(
    session: AsyncSession, mode: PreparationMode = PreparationMode.PREPARE
) -> tuple[uuid.UUID, AvatarProfile]:
    user_id, profile, asset = await _seed_candidate(session)
    await AvatarLifecycle(session).record_consent(
        profile, asset, accept=True, consent_version=CONSENT_COPY_V1
    )
    await PrepareAvatar(session, settings=_settings()).execute(user_id=user_id, mode=mode)
    return user_id, profile


async def test_prepare_enqueues_exactly_one_job(db_session: AsyncSession) -> None:
    user_id, profile = await _consent_and_prepare(db_session)

    jobs = await _jobs(db_session, PREPARATION_JOB_TYPE)
    assert len(jobs) == 1
    assert jobs[0].target_id == profile.id
    assert jobs[0].user_id == user_id
    # The stable key, never a signed URL (contract §7).
    assert jobs[0].payload is not None
    assert jobs[0].payload["source_object_key"].startswith(f"users/{user_id}/")
    assert "http" not in jobs[0].payload["source_object_key"]


async def test_a_second_prepare_while_one_runs_is_a_state_conflict(
    db_session: AsyncSession,
) -> None:
    """Contract §4.2's conflict rule — the *sequential* duplicate path.

    This is the guard a client sees. It is a read of the job table, so it cannot
    hold under a genuine race; the next test covers that half.
    """
    user_id, profile = await _consent_and_prepare(db_session)

    with pytest.raises(PreparationRefused) as refused:
        await PrepareAvatar(db_session, settings=_settings()).execute(
            user_id=user_id, mode=PreparationMode.PREPARE
        )

    assert refused.value.reason is PreparationRefusal.ALREADY_PREPARING
    assert len(await _jobs(db_session, PREPARATION_JOB_TYPE)) == 1


async def test_a_duplicate_that_slips_past_the_conflict_read_charges_once(
    db_session: AsyncSession,
) -> None:
    """Acceptance 2's real guarantee: the unique index, not the 409.

    The in-flight read is bypassed deliberately — that is exactly what a race
    does, and it is the only way to test the guard *behind* the 409. Two requests
    both reaching `enqueue` must produce one job row, i.e. one paid provider run.
    """
    user_id, profile = await _consent_and_prepare(db_session)
    use_case = PrepareAvatar(db_session, settings=_settings())
    # Simulate the loser of the race: the conflict read says "clear" (as it would
    # for a request that read before the first insert committed), so the only
    # thing standing between the user and a second charge is the key.
    use_case._preparation_in_flight = lambda _profile_id: _false()  # type: ignore[assignment]

    second = await use_case.execute(user_id=user_id, mode=PreparationMode.PREPARE)

    assert second.created is False
    assert len(await _jobs(db_session, PREPARATION_JOB_TYPE)) == 1


async def _false() -> bool:
    return False


async def test_prepare_after_a_successful_preparation_is_free(
    db_session: AsyncSession,
) -> None:
    """A completed preparation is not re-run by a repeated request.

    The job is `succeeded`, so it is no longer in flight and the 409 does not
    fire — the key is what prevents the second charge. Without it, any client
    retry after success would buy another provider call.
    """
    user_id, profile = await _consent_and_prepare(db_session)
    job = (await _jobs(db_session, PREPARATION_JOB_TYPE))[0]
    job.status = JOB_STATUS_SUCCEEDED
    await db_session.flush()

    again = await PrepareAvatar(db_session, settings=_settings()).execute(
        user_id=user_id, mode=PreparationMode.PREPARE
    )

    assert again.created is False
    assert again.job_id == job.id
    assert len(await _jobs(db_session, PREPARATION_JOB_TYPE)) == 1


async def _kill_the_preparation(
    session: AsyncSession, profile: AvatarProfile
) -> AsyncJob:
    """What the worker leaves behind once a preparation is out of attempts."""
    job = (await _jobs(session, PREPARATION_JOB_TYPE))[0]
    job.status = JOB_STATUS_DEAD
    job.attempts = job.max_attempts
    job.last_error = "RuntimeError"
    await session.flush()
    await AvatarLifecycle(session).fail_preparation(profile)
    assert profile.status == AvatarStatus.CANDIDATE.value
    return job


async def test_a_retry_after_a_dead_preparation_re_opens_the_same_job(
    db_session: AsyncSession,
) -> None:
    """The dead end, at the seam where it was created.

    `enqueue` is `ON CONFLICT DO NOTHING`, so the terminal row owned the key and
    every later `prepare` was handed a job that would never run — reported as a
    200. The row is re-opened rather than re-keyed, so the photo keeps one row,
    one cost history and one error trail.
    """
    user_id, profile = await _consent_and_prepare(db_session)
    dead = await _kill_the_preparation(db_session, profile)

    retried = await PrepareAvatar(db_session, settings=_settings()).execute(
        user_id=user_id, mode=PreparationMode.PREPARE
    )

    assert retried.created is True
    assert retried.job_id == dead.id
    assert retried.profile_status == AvatarStatus.PREPARING.value
    assert dead.status == JOB_STATUS_QUEUED
    assert dead.attempts == 0
    assert dead.last_error == "RuntimeError"
    assert profile.status == AvatarStatus.PREPARING.value
    assert len(await _jobs(db_session, PREPARATION_JOB_TYPE)) == 1


async def test_a_second_retry_does_not_re_open_an_already_revived_job(
    db_session: AsyncSession,
) -> None:
    """The conditional UPDATE, tested from the side a sequential call cannot reach.

    Sequentially the second retry never gets this far — the revived job is
    `queued`, so the in-flight read answers 409. Bypassing that read is what a
    race does, and it is the only way to exercise the guard *behind* it: the
    `status IN (dead, failed)` predicate must refuse to re-open a live job, or
    two retries would both reset `attempts` and one would report starting work
    the other had already started.

    Its limit, stated plainly: one session, so this proves the predicate, not
    that two concurrent transactions interleave safely — the same bound
    `test_a_duplicate_that_slips_past_the_conflict_read_charges_once` carries.
    """
    user_id, profile = await _consent_and_prepare(db_session)
    dead = await _kill_the_preparation(db_session, profile)
    use_case = PrepareAvatar(db_session, settings=_settings())
    first = await use_case.execute(user_id=user_id, mode=PreparationMode.PREPARE)
    use_case._preparation_in_flight = lambda _profile_id: _false()  # type: ignore[assignment]

    second = await use_case.execute(user_id=user_id, mode=PreparationMode.PREPARE)

    assert first.created is True
    assert second.created is False, "a live job must not be re-opened a second time"
    assert second.job_id == dead.id
    assert dead.status == JOB_STATUS_QUEUED
    assert len(await _jobs(db_session, PREPARATION_JOB_TYPE)) == 1


async def test_a_running_job_also_conflicts(db_session: AsyncSession) -> None:
    """Not just `queued` — a claimed job is in flight too."""
    user_id, profile = await _consent_and_prepare(db_session)
    job = (await _jobs(db_session, PREPARATION_JOB_TYPE))[0]
    job.status = JOB_STATUS_RUNNING
    await db_session.flush()

    with pytest.raises(PreparationRefused) as refused:
        await PrepareAvatar(db_session, settings=_settings()).execute(
            user_id=user_id, mode=PreparationMode.PREPARE
        )

    assert refused.value.reason is PreparationRefusal.ALREADY_PREPARING


async def test_prepare_without_consent_is_refused(db_session: AsyncSession) -> None:
    """No consent, no preparation — and no provider call either.

    The refusal is distinct from "you already have an avatar" so a client cannot
    read "skipped consent" as "already done".
    """
    user_id, profile, _asset = await _seed_candidate(db_session)

    with pytest.raises(PreparationRefused) as refused:
        await PrepareAvatar(db_session, settings=_settings()).execute(
            user_id=user_id, mode=PreparationMode.PREPARE
        )

    assert refused.value.reason is PreparationRefusal.NO_CONSENTED_SOURCE
    assert await _jobs(db_session, PREPARATION_JOB_TYPE) == []


async def test_replace_requires_an_existing_version(db_session: AsyncSession) -> None:
    """`replace` means "swap"; with nothing to swap out it is a client error."""
    user_id, profile, asset = await _seed_candidate(db_session)
    await AvatarLifecycle(db_session).record_consent(
        profile, asset, accept=True, consent_version=CONSENT_COPY_V1
    )

    with pytest.raises(PreparationRefused) as refused:
        await PrepareAvatar(db_session, settings=_settings()).execute(
            user_id=user_id, mode=PreparationMode.REPLACE
        )

    assert refused.value.reason is PreparationRefusal.WRONG_PROFILE_STATE


async def test_prepare_is_refused_on_a_ready_profile(db_session: AsyncSession) -> None:
    """The mirror image: `replace` is the endpoint for a ready avatar.

    Conflating them would make the atomic-swap guarantee depend on which URL the
    client happened to call.
    """
    user_id, profile, asset = await _seed_candidate(db_session)
    lifecycle = AvatarLifecycle(db_session)
    await lifecycle.record_consent(
        profile, asset, accept=True, consent_version=CONSENT_COPY_V1
    )
    version = AvatarVersion(
        avatar_profile_id=profile.id,
        source_asset_ids=[str(asset.id)],
        provider="fake",
        model_version="m1",
        prompt_version="p1",
        canonical_front_object_key=f"users/{user_id}/avatar/canonical/front.png",
    )
    db_session.add(version)
    await db_session.flush()
    await lifecycle.activate_version(profile, version)

    with pytest.raises(PreparationRefused) as refused:
        await PrepareAvatar(db_session, settings=_settings()).execute(
            user_id=user_id, mode=PreparationMode.PREPARE
        )

    assert refused.value.reason is PreparationRefusal.WRONG_PROFILE_STATE


async def test_a_replace_uses_the_newly_consented_photo(db_session: AsyncSession) -> None:
    """The second consent is what the replacement prepares from.

    Ordered by the consent decision, so a user who says yes to a new photo gets
    the photo they just approved — and because the source asset is in the key, the
    replacement is a genuinely new job rather than a duplicate of the first
    preparation.
    """
    user_id, profile, first_asset = await _seed_candidate(db_session)
    lifecycle = AvatarLifecycle(db_session)
    await lifecycle.record_consent(
        profile, first_asset, accept=True, consent_version=CONSENT_COPY_V1
    )
    version = AvatarVersion(
        avatar_profile_id=profile.id,
        source_asset_ids=[str(first_asset.id)],
        provider="fake",
        model_version="m1",
        prompt_version="p1",
        canonical_front_object_key=f"users/{user_id}/avatar/canonical/front.png",
    )
    db_session.add(version)
    await db_session.flush()
    await lifecycle.activate_version(profile, version)

    second_asset = AvatarSourceAsset(
        user_id=user_id,
        source_type=AvatarSourceType.USER_UPLOAD.value,
        source_object_key=f"users/{user_id}/avatar/second.jpg",
        quality_status=AvatarQualityStatus.PASSED.value,
        consent_status=AvatarConsentStatus.PENDING.value,
    )
    db_session.add(second_asset)
    await db_session.flush()
    await lifecycle.record_consent(
        profile,
        second_asset,
        accept=True,
        consent_version=CONSENT_COPY_V1,
        now=datetime.now(UTC) + timedelta(minutes=5),
    )

    started = await PrepareAvatar(db_session, settings=_settings()).execute(
        user_id=user_id, mode=PreparationMode.REPLACE
    )

    assert started.source_asset_id == second_asset.id
    assert started.created is True
    # The profile keeps its live version while the replacement prepares — no gap
    # where a ready avatar disappears (contract §4.2).
    assert profile.status == AvatarStatus.READY.value
    assert profile.active_version_id == version.id


# --- evidence 3: delete cascade / retention -------------------------------


async def _ready_profile_with_material(
    session: AsyncSession,
) -> tuple[uuid.UUID, AvatarProfile, AvatarVersion, AvatarSourceAsset]:
    user_id, profile, asset = await _seed_candidate(
        session, source_type=AvatarSourceType.USER_UPLOAD
    )
    lifecycle = AvatarLifecycle(session)
    await lifecycle.record_consent(
        profile, asset, accept=True, consent_version=CONSENT_COPY_V1
    )
    version = AvatarVersion(
        avatar_profile_id=profile.id,
        source_asset_ids=[str(asset.id)],
        provider="fake",
        model_version="m1",
        prompt_version="p1",
        canonical_front_object_key=f"users/{user_id}/avatar/canonical/front.png",
        canonical_side_object_key=f"users/{user_id}/avatar/canonical/side.png",
    )
    session.add(version)
    await session.flush()
    await lifecycle.activate_version(profile, version)
    return user_id, profile, version, asset


async def test_delete_removes_the_derived_canonical_renders(
    db_session: AsyncSession,
) -> None:
    """The derived-asset removal acceptance 3 asks for.

    The canonical renders are what the provider generated *from* the photo — the
    appearance reference itself (V3_SPEC §15's 形象参考). Every delete removes
    them: the rows in the request, the objects through the scheduled purge.
    """
    user_id, profile, version, asset = await _ready_profile_with_material(db_session)

    deleted = await DeleteAvatarProfile(db_session).execute(
        user_id=user_id, purge_derived=True
    )

    assert version.canonical_front_object_key in deleted.plan.canonical_object_keys
    assert version.canonical_side_object_key in deleted.plan.canonical_object_keys
    # Rows gone: nothing can be generated from a stale reference.
    assert await db_session.get(AvatarVersion, version.id) is None
    assert await db_session.get(AvatarSourceAsset, asset.id) is None
    # The profile survives as a tombstone, disabled and timestamped.
    assert profile.status == AvatarStatus.DISABLED.value
    assert profile.disabled_at is not None
    assert profile.active_version_id is None


async def test_delete_schedules_storage_removal_out_of_band(
    db_session: AsyncSession,
) -> None:
    """The "schedules removal" half — a job, not an inline delete.

    Object deletion is neither transactional nor reversible, so a rollback after
    a successful `delete_objects` would leave rows intact and images destroyed.
    The payload carries stable keys and never a signed URL (contract §7).
    """
    user_id, profile, version, asset = await _ready_profile_with_material(db_session)

    deleted = await DeleteAvatarProfile(db_session).execute(
        user_id=user_id, purge_derived=True
    )

    jobs = await _jobs(db_session, PURGE_JOB_TYPE)
    assert len(jobs) == 1
    assert jobs[0].id == deleted.purge_job_id
    payload = jobs[0].payload or {}
    assert set(payload["object_keys"]) == {
        asset.source_object_key,
        version.canonical_front_object_key,
        version.canonical_side_object_key,
    }
    assert payload["purge_derived"] is True
    assert all("http" not in key for key in payload["object_keys"])


async def test_an_ootd_photo_is_not_deleted_with_the_avatar(
    db_session: AsyncSession,
) -> None:
    """Unrelated user content survives.

    An OOTD photo stays in the user's own history after they delete their avatar.
    Deleting it because it once happened to be a candidate would destroy content
    they never gave us for this purpose.

    The user deliberately owns **both** kinds of material here — an OOTD photo the
    avatar merely referenced, and an uploaded photo the avatar owns. The first
    version of this test seeded only the OOTD asset, so there was nothing to purge
    at all, no job was enqueued (correctly), and the payload assertion crashed on
    an empty list. It also would not have been discriminating: "the OOTD key is
    absent" is trivially true of a purge that does not exist. With both present
    the purge job is real, and the assertion below actually distinguishes
    referenced from owned — if `AVATAR_OWNED_SOURCE_TYPES` wrongly included
    `ootd_photo`, this fails.
    """
    user_id, profile, ootd_asset = await _seed_candidate(
        db_session, source_type=AvatarSourceType.OOTD_PHOTO
    )
    owned_asset = AvatarSourceAsset(
        user_id=user_id,
        source_type=AvatarSourceType.USER_UPLOAD.value,
        source_object_key=f"users/{user_id}/avatar/uploaded.jpg",
        quality_status=AvatarQualityStatus.PASSED.value,
        consent_status=AvatarConsentStatus.PENDING.value,
    )
    db_session.add(owned_asset)
    await db_session.flush()

    deleted = await DeleteAvatarProfile(db_session).execute(
        user_id=user_id, purge_derived=True
    )

    assert ootd_asset.source_object_key in deleted.plan.retained_object_keys
    assert ootd_asset.source_object_key not in deleted.plan.source_object_keys
    assert owned_asset.source_object_key in deleted.plan.source_object_keys

    purge_payload = (await _jobs(db_session, PURGE_JOB_TYPE))[0].payload or {}
    assert owned_asset.source_object_key in purge_payload["object_keys"]
    assert ootd_asset.source_object_key not in purge_payload["object_keys"]


async def test_nothing_to_purge_enqueues_no_job(db_session: AsyncSession) -> None:
    """A delete with only *referenced* material schedules no storage sweep.

    Found while fixing the test above, and worth pinning rather than leaving
    implicit: a user whose only candidate was an OOTD photo owns no avatar
    objects, so there is nothing for the purge worker to do and no job is
    created. Enqueuing an empty sweep would put a row on the queue for every
    such deletion and make "a purge is pending" meaningless as a signal.
    """
    user_id, profile, asset = await _seed_candidate(
        db_session, source_type=AvatarSourceType.OOTD_PHOTO
    )

    deleted = await DeleteAvatarProfile(db_session).execute(
        user_id=user_id, purge_derived=True
    )

    assert deleted.purge_job_id is None
    assert deleted.deleted_object_count == 0
    assert await _jobs(db_session, PURGE_JOB_TYPE) == []
    # The row is still deleted and the photo still retained — "no purge job" is
    # not "nothing happened".
    assert await db_session.get(AvatarSourceAsset, asset.id) is None
    assert asset.source_object_key in deleted.plan.retained_object_keys


async def test_purge_derived_is_recorded_on_both_branches(
    db_session: AsyncSession,
) -> None:
    """The caller's explicit choice is persisted, per V3_SPEC §13.5.

    §13.5 requires being unambiguous about whether derived assets go too, which
    means the decision has to be recoverable afterwards rather than living only in
    a request that is already gone.
    """
    for purge_derived in (True, False):
        user_id, profile, version, asset = await _ready_profile_with_material(db_session)

        deleted = await DeleteAvatarProfile(db_session).execute(
            user_id=user_id, purge_derived=purge_derived
        )

        assert deleted.plan.purge_derived is purge_derived
        job = next(
            j
            for j in await _jobs(db_session, PURGE_JOB_TYPE)
            if (j.payload or {}).get("avatar_profile_id") == str(profile.id)
        )
        assert (job.payload or {})["purge_derived"] is purge_derived


async def test_purge_derived_try_on_scope_is_empty_by_construction(
    db_session: AsyncSession,
) -> None:
    """Why `purge_derived` has no try-on effect yet — asserted, not just claimed.

    Contract §4.2 scopes the parameter to "generated **try-on** assets derived
    from any avatar version". No such asset can exist: `visual-preview` is not
    implemented (V3-016/V3-017) and `outfit_visual_assets` has no column linking a
    row to an avatar version — V3-011's `AvatarDeletionPlan` docstring assigned
    that column to V3-018.

    This asserts the *premise* rather than the behavior, so the day someone adds
    the link this test fails and points at
    `app/application/avatar/delete_avatar.py` instead of leaving a parameter that
    silently does nothing. A behavioral test over a hand-seeded row would only
    prove that a query compiles.
    """
    columns = set(OutfitVisualAsset.__table__.c.keys())

    assert "avatar_version_id" not in columns, (
        "outfit_visual_assets now links to an avatar version — `purge_derived` "
        "must start deleting those rows; see delete_avatar.py"
    )
    # And nothing writes the V3 try-on visual type yet, so there is nothing to
    # find even without a link column.
    existing = await db_session.scalar(
        sa.select(sa.func.count())
        .select_from(OutfitVisualAsset)
        .where(OutfitVisualAsset.visual_type == "static_tryon")
    )
    assert existing == 0


async def test_deleting_twice_is_idempotent(db_session: AsyncSession) -> None:
    """A repeated delete neither errors nor enqueues a second sweep.

    The second call finds the rows already gone, so its plan is empty and no job
    is created — and the tombstone keeps its *original* `disabled_at`, because
    "when did the user turn this off" must not drift forward on a retry.
    """
    user_id, profile, version, asset = await _ready_profile_with_material(db_session)
    first = await DeleteAvatarProfile(db_session).execute(
        user_id=user_id, purge_derived=True
    )
    disabled_at = profile.disabled_at

    second = await DeleteAvatarProfile(db_session).execute(
        user_id=user_id, purge_derived=True
    )

    assert first.purge_job_id is not None
    assert second.purge_job_id is None
    assert second.deleted_object_count == 0
    assert profile.disabled_at == disabled_at
    assert len(await _jobs(db_session, PURGE_JOB_TYPE)) == 1


async def test_deleting_a_rebuilt_avatar_schedules_its_own_purge(
    db_session: AsyncSession,
) -> None:
    """Codex review on BE#147, P1 — the completed job must not swallow it.

    The profile id survives a deletion (the tombstone keeps it) and the frozen
    `disabled -> preparing` re-enable reuses it, so `avatar_purge:{profile_id}`
    was not unique to one deletion. Once the first purge had SUCCEEDED, the
    second deletion's `enqueue` hit that key, `ON CONFLICT DO NOTHING` returned
    the finished job without replacing its payload, and the rebuilt avatar's
    photo and canonical render were never swept — while the response reported
    them as scheduled. Person material retained after a deletion request, and
    reported as deleted.
    """
    user_id, profile, version, asset = await _ready_profile_with_material(db_session)
    first = await DeleteAvatarProfile(db_session).execute(user_id=user_id, purge_derived=True)
    first_job = (await _jobs(db_session, PURGE_JOB_TYPE))[0]
    # The first sweep finishes, which is what made the old key permanent.
    first_job.status = JOB_STATUS_SUCCEEDED
    await db_session.flush()

    # The user comes back: new photo, new consent, new version, live again.
    rebuilt_asset = AvatarSourceAsset(
        user_id=user_id,
        source_type=AvatarSourceType.USER_UPLOAD.value,
        source_object_key=f"users/{user_id}/avatar/rebuilt-source.jpg",
        quality_status=AvatarQualityStatus.PASSED.value,
        consent_status=AvatarConsentStatus.PENDING.value,
    )
    db_session.add(rebuilt_asset)
    await db_session.flush()
    lifecycle = AvatarLifecycle(db_session)
    await lifecycle.record_consent(
        profile, rebuilt_asset, accept=True, consent_version=CONSENT_COPY_V1
    )
    rebuilt_version = AvatarVersion(
        avatar_profile_id=profile.id,
        source_asset_ids=[str(rebuilt_asset.id)],
        provider="fake",
        model_version="m2",
        prompt_version="p1",
        canonical_front_object_key=f"users/{user_id}/avatar/canonical/rebuilt-front.png",
    )
    db_session.add(rebuilt_version)
    await db_session.flush()
    await lifecycle.activate_version(profile, rebuilt_version)

    second = await DeleteAvatarProfile(db_session).execute(user_id=user_id, purge_derived=True)

    assert second.purge_job_id is not None
    assert second.purge_job_id != first.purge_job_id
    jobs = await _jobs(db_session, PURGE_JOB_TYPE)
    assert len(jobs) == 2
    scheduled = next(job for job in jobs if job.id == second.purge_job_id)
    assert scheduled.status == JOB_STATUS_QUEUED
    keys = scheduled.payload["object_keys"] if scheduled.payload else []
    assert rebuilt_asset.source_object_key in keys
    assert rebuilt_version.canonical_front_object_key in keys
    # And the response is not lying about what will be swept.
    assert second.deleted_object_count == len(keys)


async def test_delete_without_a_profile_is_not_found(db_session: AsyncSession) -> None:
    user_id = await _seed_user(db_session)

    with pytest.raises(AvatarProfileNotFound):
        await DeleteAvatarProfile(db_session).execute(user_id=user_id, purge_derived=True)


async def test_delete_does_not_touch_another_users_avatar(
    db_session: AsyncSession,
) -> None:
    """Scoped by user, so one deletion cannot reach into another person's data."""
    _mine, my_profile, my_version, my_asset = await _ready_profile_with_material(db_session)
    other_id, other_profile, other_version, other_asset = await _ready_profile_with_material(
        db_session
    )

    await DeleteAvatarProfile(db_session).execute(user_id=_mine, purge_derived=True)

    assert await db_session.get(AvatarVersion, other_version.id) is not None
    assert await db_session.get(AvatarSourceAsset, other_asset.id) is not None
    assert other_profile.status == AvatarStatus.READY.value


async def test_a_failed_preparation_returns_the_profile_to_candidate(
    db_session: AsyncSession,
) -> None:
    """Contract §4.1's `preparing → candidate`, so a retry is available.

    The accepted consent is deliberately *not* reverted: consent records what the
    user agreed to, and rewriting it because our provider failed would falsify the
    audit trail acceptance 1 exists to protect.

    This is the transition in isolation; the worker calls it only once the job is
    out of attempts, and `test_a_dead_preparation_is_retried_and_reaches_ready`
    (worker tests) is where "a retry is available" is actually collected on.
    """
    user_id, profile, asset = await _seed_candidate(db_session)
    lifecycle = AvatarLifecycle(db_session)
    await lifecycle.record_consent(
        profile, asset, accept=True, consent_version=CONSENT_COPY_V1
    )
    assert profile.status == AvatarStatus.PREPARING.value

    moved = await lifecycle.fail_preparation(profile)

    assert moved is True
    assert profile.status == AvatarStatus.CANDIDATE.value
    assert asset.consent_status == AvatarConsentStatus.ACCEPTED.value
    assert profile.consent_version == CONSENT_COPY_V1


async def test_only_a_candidate_profile_re_enters_preparation(
    db_session: AsyncSession,
) -> None:
    """`begin_preparation`'s bound *is* the guarantee, so it is tested as one.

    Widening it by one status would break something specific in each case:
    `ready`/`refresh_recommended` would strip `active_version_id` through the
    `active_version_matches_status` CHECK — the gap contract §4.2 forbids, and
    exactly what a `replace` retry would hit — and `disabled` would have to erase
    `disabled_at` to satisfy `disabled_status_has_timestamp`, deleting the
    record of an explicit revocation at the moment a preparation *starts*.
    """
    lifecycle = AvatarLifecycle(db_session)

    # `candidate` — the retry path, the only one that moves.
    _user_id, candidate_profile, _asset = await _seed_candidate(db_session)
    assert await lifecycle.begin_preparation(candidate_profile) is True
    assert candidate_profile.status == AvatarStatus.PREPARING.value

    # `ready` — where `replace` retries from. The live version must survive.
    _ready_user, ready_profile, version, _ready_asset = await _ready_profile_with_material(
        db_session
    )
    assert await lifecycle.begin_preparation(ready_profile) is False
    assert ready_profile.status == AvatarStatus.READY.value
    assert ready_profile.active_version_id == version.id

    # `disabled` — the revocation timestamp must survive.
    await lifecycle.disable(ready_profile)
    assert ready_profile.status == AvatarStatus.DISABLED.value
    disabled_at = ready_profile.disabled_at
    assert disabled_at is not None
    assert await lifecycle.begin_preparation(ready_profile) is False
    assert ready_profile.status == AvatarStatus.DISABLED.value
    assert ready_profile.disabled_at == disabled_at

    # `none` — nothing to prepare from.
    none_user = await _seed_user(db_session)
    none_profile = AvatarProfile(user_id=none_user, status=AvatarStatus.NONE.value)
    db_session.add(none_profile)
    await db_session.flush()
    assert await lifecycle.begin_preparation(none_profile) is False
    assert none_profile.status == AvatarStatus.NONE.value


async def test_a_failed_replace_leaves_the_live_version_alone(
    db_session: AsyncSession,
) -> None:
    """"Replace failure keeps the previous version active" (contract §4.2).

    Proven as a no-op rather than as a compensating write: `fail_preparation`
    declines to move a profile that is not `preparing`, which is exactly the state
    a ready profile is in while a replacement prepares alongside it.
    """
    user_id, profile, version, asset = await _ready_profile_with_material(db_session)

    moved = await AvatarLifecycle(db_session).fail_preparation(profile)

    assert moved is False
    assert profile.status == AvatarStatus.READY.value
    assert profile.active_version_id == version.id
