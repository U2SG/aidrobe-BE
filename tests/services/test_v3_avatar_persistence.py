"""V3-011 — avatar persistence and lifecycle against a real Postgres.

What only a database can prove, and is therefore here rather than in a unit
test:

- the three record kinds are genuinely independent — material exists without
  consent, a version outlives the photo it was built from, and revoking does
  not erase history;
- the CHECK constraints actually refuse the illegal states, including the one
  that matters most: you cannot delete the version a ready profile is pointing
  at, so a ready avatar cannot silently disappear (contract §4.2);
- delete and disable do exactly what they claim, including *not* deleting the
  user's own OOTD photo just because the avatar once referenced it;
- the account-level PIPL purge still cascades everything away.

All image references written here are storage object keys. Nothing signed is
persisted (acceptance 2), which `test_no_column_can_hold_an_expiring_url`
enforces structurally.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import (
    AvatarConsentStatus,
    AvatarQualityStatus,
    AvatarSourceType,
    AvatarStatus,
)
from app.infrastructure.persistence.models.avatar import (
    AvatarProfile,
    AvatarSourceAsset,
    AvatarVersion,
)
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.repositories.avatar_repo import (
    AvatarLifecycle,
    SqlAlchemyAvatarProfileRepository,
    SqlAlchemyAvatarSourceAssetRepository,
)

CONSENT_COPY_V1 = "avatar_consent_placeholder_v1"
CONSENT_COPY_V2 = "avatar_consent_placeholder_v2"


async def _seed_user(session: AsyncSession) -> uuid.UUID:
    user_id = uuid.uuid4()
    session.add(User(id=user_id))
    await session.flush()
    return user_id


async def _add_asset(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    object_key: str,
    source_type: AvatarSourceType = AvatarSourceType.CAMERA_CAPTURE,
    quality_status: AvatarQualityStatus = AvatarQualityStatus.PASSED,
) -> AvatarSourceAsset:
    asset = AvatarSourceAsset(
        user_id=user_id,
        source_type=source_type.value,
        source_object_key=object_key,
        quality_status=quality_status.value,
        consent_status=AvatarConsentStatus.PENDING.value,
    )
    return await SqlAlchemyAvatarSourceAssetRepository(session).add(asset)


async def _add_profile(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    status: AvatarStatus = AvatarStatus.CANDIDATE,
) -> AvatarProfile:
    profile = AvatarProfile(user_id=user_id, status=status.value)
    return await SqlAlchemyAvatarProfileRepository(session).add(profile)


async def _add_version(
    session: AsyncSession,
    profile: AvatarProfile,
    *,
    front_key: str,
    side_key: str | None = None,
    source_asset_ids: list[str] | None = None,
) -> AvatarVersion:
    version = AvatarVersion(
        avatar_profile_id=profile.id,
        source_asset_ids=source_asset_ids or [],
        provider="fake",
        model_version="test-model-1",
        prompt_version="test-prompt-1",
        canonical_front_object_key=front_key,
        canonical_side_object_key=side_key,
        quality_status=AvatarQualityStatus.PASSED.value,
    )
    return await SqlAlchemyAvatarProfileRepository(session).add_version(version)


# --- acceptance 1: three separate records ---------------------------------


async def test_material_exists_without_consent_and_without_a_version(db_session: AsyncSession) -> None:
    """Noticing a photo grants nothing.

    This is the source-B rule with teeth (contract §4.3): an `outfit_import`
    photo becoming a candidate row must not imply person-reference consent,
    and must not conjure a derived avatar.
    """
    user_id = await _seed_user(db_session)
    asset = await _add_asset(
        db_session,
        user_id,
        object_key="users/u/avatar-src/imported.jpg",
        source_type=AvatarSourceType.OUTFIT_IMPORT,
    )
    profile = await _add_profile(db_session, user_id)

    assert asset.consent_status == AvatarConsentStatus.PENDING.value
    assert asset.consent_version is None
    assert asset.consent_decided_at is None
    assert profile.consent_version is None
    assert profile.consented_at is None
    assert profile.active_version_id is None
    assert await SqlAlchemyAvatarProfileRepository(db_session).list_versions(profile.id) == []


async def test_accepting_records_the_copy_version_on_both_records(db_session: AsyncSession) -> None:
    """The asset records what *this photo* was accepted under; the profile
    records the consent currently in force. Both are written by one method, so
    they cannot drift."""
    user_id = await _seed_user(db_session)
    asset = await _add_asset(db_session, user_id, object_key="users/u/avatar-src/a.jpg")
    profile = await _add_profile(db_session, user_id)
    decided_at = datetime(2026, 8, 14, 9, 0, tzinfo=UTC)

    await AvatarLifecycle(db_session).record_consent(
        profile, asset, accept=True, consent_version=CONSENT_COPY_V1, now=decided_at
    )

    assert asset.consent_status == AvatarConsentStatus.ACCEPTED.value
    assert asset.consent_version == CONSENT_COPY_V1
    assert asset.consent_decided_at == decided_at
    assert profile.consent_version == CONSENT_COPY_V1
    assert profile.consented_at == decided_at
    # candidate → preparing on explicit consent (contract §4.1).
    assert profile.status == AvatarStatus.PREPARING.value


async def test_a_later_copy_revision_cannot_rewrite_an_earlier_acceptance(db_session: AsyncSession) -> None:
    """Per-asset consent versions are the audit record; the profile mirrors the
    latest. If the asset's version were derived from the profile, revising the
    copy would retroactively claim the user had seen text they never saw."""
    user_id = await _seed_user(db_session)
    first = await _add_asset(db_session, user_id, object_key="users/u/avatar-src/first.jpg")
    second = await _add_asset(db_session, user_id, object_key="users/u/avatar-src/second.jpg")
    profile = await _add_profile(db_session, user_id)
    lifecycle = AvatarLifecycle(db_session)

    await lifecycle.record_consent(profile, first, accept=True, consent_version=CONSENT_COPY_V1)
    await lifecycle.record_consent(profile, second, accept=True, consent_version=CONSENT_COPY_V2)

    assert first.consent_version == CONSENT_COPY_V1
    assert second.consent_version == CONSENT_COPY_V2
    assert profile.consent_version == CONSENT_COPY_V2


async def test_accepting_without_a_copy_version_is_refused(db_session: AsyncSession) -> None:
    user_id = await _seed_user(db_session)
    asset = await _add_asset(db_session, user_id, object_key="users/u/avatar-src/a.jpg")
    profile = await _add_profile(db_session, user_id)

    with pytest.raises(ValueError):
        await AvatarLifecycle(db_session).record_consent(profile, asset, accept=True, consent_version=None)


async def test_declining_is_terminal_and_records_no_consent(db_session: AsyncSession) -> None:
    user_id = await _seed_user(db_session)
    asset = await _add_asset(db_session, user_id, object_key="users/u/avatar-src/a.jpg")
    profile = await _add_profile(db_session, user_id)

    await AvatarLifecycle(db_session).record_consent(profile, asset, accept=False)

    assert asset.consent_status == AvatarConsentStatus.DECLINED.value
    assert asset.consent_version is None
    assert asset.consent_decided_at is not None
    # Declining one photo is not revoking the avatar.
    assert profile.status == AvatarStatus.NONE.value
    assert profile.disabled_at is None
    assert profile.consent_version is None


async def test_a_declined_photo_cannot_be_offered_a_second_time(db_session: AsyncSession) -> None:
    """"每张候选只提示一次" (V3_SPEC §7.3) enforced by the database, not by
    V3-012 remembering to check."""
    user_id = await _seed_user(db_session)
    object_key = "users/u/avatar-src/declined.jpg"
    asset = await _add_asset(db_session, user_id, object_key=object_key)
    profile = await _add_profile(db_session, user_id)
    await AvatarLifecycle(db_session).record_consent(profile, asset, accept=False)

    with pytest.raises(IntegrityError):
        await _add_asset(db_session, user_id, object_key=object_key)


async def test_the_same_photo_from_two_users_is_two_rows(db_session: AsyncSession) -> None:
    """The uniqueness is per user — object keys are user-scoped, and one
    user's decision must never suppress another's prompt."""
    first_user = await _seed_user(db_session)
    second_user = await _seed_user(db_session)
    key = "shared/looking/key.jpg"

    await _add_asset(db_session, first_user, object_key=key)
    await _add_asset(db_session, second_user, object_key=key)

    repo = SqlAlchemyAvatarSourceAssetRepository(db_session)
    assert await repo.get_by_object_key(key, first_user) is not None
    assert await repo.get_by_object_key(key, second_user) is not None


async def test_a_user_has_at_most_one_avatar_profile(db_session: AsyncSession) -> None:
    user_id = await _seed_user(db_session)
    await _add_profile(db_session, user_id)

    with pytest.raises(IntegrityError):
        await _add_profile(db_session, user_id)


# --- acceptance 2: stable object keys, never expiring URLs -----------------


def test_no_column_can_hold_an_expiring_url() -> None:
    """Structural, so the guarantee cannot decay into a convention.

    Every image reference on these three tables is an object key. A signed URL
    is TTL-bound; persisting one produces a row that is silently wrong after
    30 minutes. Read paths sign on demand instead.
    """
    for table in (AvatarSourceAsset.__table__, AvatarProfile.__table__, AvatarVersion.__table__):
        offenders = [column.name for column in table.columns if "url" in column.name.lower()]
        assert offenders == [], f"{table.name} would persist an expiring URL: {offenders}"


async def test_object_keys_round_trip_unchanged(db_session: AsyncSession) -> None:
    user_id = await _seed_user(db_session)
    profile = await _add_profile(db_session, user_id)
    asset = await _add_asset(db_session, user_id, object_key="users/u/avatar-src/raw.jpg")
    version = await _add_version(
        db_session,
        profile,
        front_key="users/u/avatar-canonical/front.png",
        side_key="users/u/avatar-canonical/side.png",
        source_asset_ids=[str(asset.id)],
    )
    ids = (asset.id, version.id)
    db_session.expunge_all()

    reloaded_asset = await db_session.get(AvatarSourceAsset, ids[0])
    reloaded_version = await db_session.get(AvatarVersion, ids[1])
    assert reloaded_asset is not None and reloaded_version is not None
    assert reloaded_asset.source_object_key == "users/u/avatar-src/raw.jpg"
    assert reloaded_version.canonical_front_object_key == "users/u/avatar-canonical/front.png"
    assert reloaded_version.canonical_side_object_key == "users/u/avatar-canonical/side.png"


# --- the state machine the CHECK constraints enforce -----------------------


async def test_a_ready_profile_without_an_active_version_is_rejected(db_session: AsyncSession) -> None:
    """The frozen GET shape says `active_version` is non-null exactly when the
    status is ready / refresh_recommended. A row that contradicts it would make
    the response unrepresentable."""
    user_id = await _seed_user(db_session)
    profile = await _add_profile(db_session, user_id)
    profile.status = AvatarStatus.READY.value

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_a_candidate_profile_pointing_at_a_version_is_rejected(db_session: AsyncSession) -> None:
    """The other direction of the same constraint."""
    user_id = await _seed_user(db_session)
    profile = await _add_profile(db_session, user_id)
    version = await _add_version(db_session, profile, front_key="users/u/avatar-canonical/front.png")
    profile.active_version_id = version.id

    with pytest.raises(IntegrityError):
        await db_session.flush()


async def test_activating_a_new_version_retires_the_old_one_atomically(db_session: AsyncSession) -> None:
    """`replace` is a swap, never a mutation: the old canonical assets are
    still on disk and still described by a row, which is what makes a failed
    replace harmless."""
    user_id = await _seed_user(db_session)
    profile = await _add_profile(db_session, user_id)
    lifecycle = AvatarLifecycle(db_session)
    first = await _add_version(db_session, profile, front_key="users/u/avatar-canonical/v1.png")
    await lifecycle.activate_version(profile, first)

    second = await _add_version(db_session, profile, front_key="users/u/avatar-canonical/v2.png")
    swapped_at = datetime(2026, 8, 14, 10, 0, tzinfo=UTC)
    await lifecycle.activate_version(profile, second, now=swapped_at)

    assert profile.active_version_id == second.id
    assert profile.status == AvatarStatus.READY.value
    assert second.retired_at is None
    assert first.retired_at == swapped_at
    # Nothing was deleted: the previous version is still there to fall back on.
    assert len(await SqlAlchemyAvatarProfileRepository(db_session).list_versions(profile.id)) == 2


async def test_consenting_to_new_material_while_ready_keeps_the_current_avatar_live(
    db_session: AsyncSession,
) -> None:
    """The `replace` flow must not take the working avatar away.

    The frozen transition list (contract §4.1) has no `ready → preparing`. If
    consent moved a ready profile to `preparing`, the
    `active_version_matches_status` CHECK would force `active_version_id` to
    NULL — the user would lose a usable avatar for the duration of a
    preparation that may then fail, which is the gap §4.2 forbids.
    """
    user_id = await _seed_user(db_session)
    first = await _add_asset(db_session, user_id, object_key="users/u/avatar-src/first.jpg")
    profile = await _add_profile(db_session, user_id)
    lifecycle = AvatarLifecycle(db_session)
    await lifecycle.record_consent(profile, first, accept=True, consent_version=CONSENT_COPY_V1)
    live = await _add_version(db_session, profile, front_key="users/u/avatar-canonical/v1.png")
    await lifecycle.activate_version(profile, live)

    replacement_source = await _add_asset(db_session, user_id, object_key="users/u/avatar-src/better.jpg")
    await lifecycle.record_consent(profile, replacement_source, accept=True, consent_version=CONSENT_COPY_V2)

    assert profile.status == AvatarStatus.READY.value
    assert profile.active_version_id == live.id
    assert profile.consent_version == CONSENT_COPY_V2
    assert live.retired_at is None


async def test_the_active_version_cannot_be_deleted_from_under_a_ready_profile(
    db_session: AsyncSession,
) -> None:
    """The constraint pair doing its job.

    `active_version_id` is ON DELETE SET NULL, so deleting the live version
    would null the pointer — and nulling it while the status is `ready`
    violates `active_version_matches_status`. The delete therefore fails
    outright, which is the database refusing to let a ready avatar silently
    disappear (contract §4.2). Disabling or swapping first is the only route.
    """
    user_id = await _seed_user(db_session)
    profile = await _add_profile(db_session, user_id)
    version = await _add_version(db_session, profile, front_key="users/u/avatar-canonical/v1.png")
    await AvatarLifecycle(db_session).activate_version(profile, version)

    with pytest.raises(IntegrityError):
        await db_session.execute(sa.delete(AvatarVersion).where(AvatarVersion.id == version.id))
        await db_session.flush()


# --- acceptance 3: delete / disable semantics -----------------------------


async def test_disable_blocks_generation_but_deletes_nothing(db_session: AsyncSession) -> None:
    """Revocation is a state change, not an erasure: new generation is
    impossible (no active version), while consent, material and history all
    remain — "existing decisions remain readable" (contract §4.3)."""
    user_id = await _seed_user(db_session)
    asset = await _add_asset(db_session, user_id, object_key="users/u/avatar-src/a.jpg")
    profile = await _add_profile(db_session, user_id)
    lifecycle = AvatarLifecycle(db_session)
    await lifecycle.record_consent(profile, asset, accept=True, consent_version=CONSENT_COPY_V1)
    version = await _add_version(db_session, profile, front_key="users/u/avatar-canonical/v1.png")
    await lifecycle.activate_version(profile, version)

    revoked_at = datetime(2026, 8, 14, 11, 0, tzinfo=UTC)
    await lifecycle.disable(profile, now=revoked_at)

    assert profile.status == AvatarStatus.DISABLED.value
    assert profile.disabled_at == revoked_at
    assert profile.active_version_id is None
    assert version.retired_at == revoked_at
    # Everything survives.
    assert await db_session.get(AvatarVersion, version.id) is not None
    assert await db_session.get(AvatarSourceAsset, asset.id) is not None
    assert profile.consent_version == CONSENT_COPY_V1


async def test_revoking_twice_keeps_the_original_revocation_timestamp(db_session: AsyncSession) -> None:
    """`disabled_at` answers "when did the user turn this off". Re-entering the
    disabled path — which `delete_avatar` does on its way through — must not
    move that answer forward."""
    user_id = await _seed_user(db_session)
    profile = await _add_profile(db_session, user_id)
    lifecycle = AvatarLifecycle(db_session)
    first_revocation = datetime(2026, 8, 14, 11, 0, tzinfo=UTC)

    await lifecycle.disable(profile, now=first_revocation)
    await lifecycle.disable(profile, now=first_revocation + timedelta(days=2))

    assert profile.disabled_at == first_revocation


async def test_a_disabled_profile_is_re_enabled_by_a_new_consent(db_session: AsyncSession) -> None:
    """`disabled → preparing` (contract §4.1) — not a stuck state."""
    user_id = await _seed_user(db_session)
    first = await _add_asset(db_session, user_id, object_key="users/u/avatar-src/first.jpg")
    profile = await _add_profile(db_session, user_id)
    lifecycle = AvatarLifecycle(db_session)
    await lifecycle.record_consent(profile, first, accept=True, consent_version=CONSENT_COPY_V1)
    await lifecycle.disable(profile)

    second = await _add_asset(db_session, user_id, object_key="users/u/avatar-src/second.jpg")
    await lifecycle.record_consent(profile, second, accept=True, consent_version=CONSENT_COPY_V2)

    assert profile.status == AvatarStatus.PREPARING.value
    assert profile.disabled_at is None
    assert profile.consent_version == CONSENT_COPY_V2


async def test_delete_removes_the_material_and_keeps_the_revocation_record(
    db_session: AsyncSession,
) -> None:
    """"Disable and delete" (contract §4.2): the photos and derived versions
    go, the fact that the user turned it off stays."""
    user_id = await _seed_user(db_session)
    asset = await _add_asset(db_session, user_id, object_key="users/u/avatar-src/upload.jpg")
    profile = await _add_profile(db_session, user_id)
    lifecycle = AvatarLifecycle(db_session)
    await lifecycle.record_consent(profile, asset, accept=True, consent_version=CONSENT_COPY_V1)
    version = await _add_version(
        db_session,
        profile,
        front_key="users/u/avatar-canonical/front.png",
        side_key="users/u/avatar-canonical/side.png",
        source_asset_ids=[str(asset.id)],
    )
    await lifecycle.activate_version(profile, version)

    plan = await lifecycle.delete_avatar(profile, purge_derived=True)

    assert plan.profile_disabled is True
    assert plan.purge_derived is True
    assert plan.source_object_keys == ("users/u/avatar-src/upload.jpg",)
    assert set(plan.canonical_object_keys) == {
        "users/u/avatar-canonical/front.png",
        "users/u/avatar-canonical/side.png",
    }
    assert await db_session.get(AvatarVersion, version.id) is None
    assert await db_session.get(AvatarSourceAsset, asset.id) is None
    # The tombstone: revocation is recorded, and coming back is still possible.
    surviving = await SqlAlchemyAvatarProfileRepository(db_session).get_for_user(user_id)
    assert surviving is not None
    assert surviving.status == AvatarStatus.DISABLED.value
    assert surviving.disabled_at is not None
    assert surviving.active_version_id is None


async def test_delete_does_not_destroy_photos_the_avatar_only_referenced(
    db_session: AsyncSession,
) -> None:
    """The distinction acceptance 3 is really about.

    An `ootd_photo` candidate points at an object the OOTD feature owns.
    Deleting the avatar drops the reference; deleting the user's OOTD photo out
    of their own history would be a data-loss bug wearing a privacy costume.
    Only `user_upload` / `camera_capture` material is avatar-owned.
    """
    user_id = await _seed_user(db_session)
    owned = await _add_asset(
        db_session,
        user_id,
        object_key="users/u/avatar-src/captured.jpg",
        source_type=AvatarSourceType.CAMERA_CAPTURE,
    )
    referenced = await _add_asset(
        db_session,
        user_id,
        object_key="users/u/ootd/2026-08-01.jpg",
        source_type=AvatarSourceType.OOTD_PHOTO,
    )
    profile = await _add_profile(db_session, user_id)

    plan = await AvatarLifecycle(db_session).delete_avatar(profile, purge_derived=False)

    assert plan.source_object_keys == (owned.source_object_key,)
    assert plan.retained_object_keys == (referenced.source_object_key,)
    assert plan.purge_derived is False


async def test_a_version_outlives_the_source_photo_it_was_built_from(db_session: AsyncSession) -> None:
    """Provenance is JSONB, not a foreign key, precisely so retention can purge
    the sensitive input without cascading away the record of what was made
    from it."""
    user_id = await _seed_user(db_session)
    profile = await _add_profile(db_session, user_id)
    asset = await _add_asset(db_session, user_id, object_key="users/u/avatar-src/a.jpg")
    version = await _add_version(
        db_session,
        profile,
        front_key="users/u/avatar-canonical/front.png",
        source_asset_ids=[str(asset.id)],
    )
    asset_id = str(asset.id)
    version_id = version.id

    await db_session.execute(sa.delete(AvatarSourceAsset).where(AvatarSourceAsset.id == asset.id))
    await db_session.flush()
    db_session.expunge_all()

    surviving = await db_session.get(AvatarVersion, version_id)
    assert surviving is not None
    assert surviving.source_asset_ids == [asset_id]


async def test_deleting_the_user_cascades_every_avatar_record(db_session: AsyncSession) -> None:
    """The account-level PIPL purge (`DELETE /v1/me`) needs no avatar-specific
    code: the `users` FK cascade is the whole mechanism."""
    user_id = await _seed_user(db_session)
    await _add_asset(db_session, user_id, object_key="users/u/avatar-src/a.jpg")
    profile = await _add_profile(db_session, user_id)
    await _add_version(db_session, profile, front_key="users/u/avatar-canonical/front.png")

    await db_session.execute(sa.delete(User).where(User.id == user_id))
    await db_session.flush()

    for model in (AvatarSourceAsset, AvatarProfile, AvatarVersion):
        remaining = await db_session.scalar(sa.select(sa.func.count()).select_from(model))
        assert remaining == 0


async def test_listing_is_scoped_to_the_owner(db_session: AsyncSession) -> None:
    """Contract §3: a wrong-owner read returns nothing, so the caller answers
    404 without leaking that the row exists."""
    owner = await _seed_user(db_session)
    stranger = await _seed_user(db_session)
    asset = await _add_asset(db_session, owner, object_key="users/u/avatar-src/a.jpg")

    repo = SqlAlchemyAvatarSourceAssetRepository(db_session)
    assert await repo.get_for_user(asset.id, owner) is not None
    assert await repo.get_for_user(asset.id, stranger) is None
    assert await repo.list_for_user(stranger) == []


async def test_retention_reads_the_real_created_at(db_session: AsyncSession) -> None:
    """The retention policy is anchored on a server timestamp, not on a value
    the client can choose — so the purge clock cannot be pushed out by a
    caller."""
    from app.application.avatar.retention import source_asset_purge_due_at
    from app.config.settings import Settings

    user_id = await _seed_user(db_session)
    asset = await _add_asset(db_session, user_id, object_key="users/u/avatar-src/a.jpg")
    await db_session.refresh(asset)

    settings = Settings(AVATAR_SOURCE_RETENTION_DAYS=30)
    due = source_asset_purge_due_at(asset.created_at, settings)
    assert due is not None
    assert due == asset.created_at + timedelta(days=30)
