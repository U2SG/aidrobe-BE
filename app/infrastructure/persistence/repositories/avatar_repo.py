"""V3-011 — avatar repositories and the explicit disable/delete lifecycle.

Reads scope every query by `user_id`, so a wrong-owner lookup returns None and
the caller answers 404 without leaking existence (contract §3) — the V3-003
convention.

`AvatarLifecycle` holds the state transitions rather than leaving them to be
re-derived at each endpoint. They are persistence semantics, not endpoint
policy: `avatar_profiles` carries a CHECK that ties `active_version_id` to
`status`, so there is exactly one legal way to make a profile ready or to take
a version out of service, and it lives here where V3-013 can only use it
correctly.

What lives elsewhere on purpose:

- **Prepare / replace jobs** (V3-013) — this module never calls a provider and
  never enqueues work. `activate_version` is the *landing* of a preparation
  that already succeeded.
- **Candidate detection** (V3-012) — `app/application/avatar/detect_candidate.py`
  decides *whether* a photo is worth offering; the `none → candidate`
  transition it produces lands in `offer_candidate` below, which cannot grant
  consent because consent is not one of its parameters.
- **Derived try-on asset purge** (V3-013/V3-018) — see `AvatarDeletionPlan`.
"""

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import (
    AVATAR_OWNED_SOURCE_TYPES,
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


@dataclass(frozen=True)
class AvatarDeletionPlan:
    """What a `delete_avatar` call removed, and what storage must still remove.

    The rows are already gone when this is returned; the object keys are what
    the caller (V3-013's `DELETE /v1/avatar-profile`) hands to the storage
    port. Splitting it this way keeps object deletion — which is not
    transactional and not reversible — outside the DB transaction, so a
    rollback can never leave the rows intact but the images destroyed.

    `purge_derived` is carried, not acted on. The frozen contract §4.2 makes it
    a **required** query parameter with no default, and requiring it here means
    V3-013 cannot forget to decide. Its actual effect — also deleting generated
    try-on assets derived from an avatar version — needs the
    `outfit_visual_assets → avatar_versions` link, which does not exist yet
    (V3-018 adds `visual_type=static_tryon` and its avatar_version_id per
    V3_SPEC §11.3). V3-013's acceptance 3 owns that behavior. Everything in
    this plan is what V3-011 genuinely deletes today.
    """

    profile_disabled: bool
    purge_derived: bool
    #: Objects the avatar owns and that must be deleted from storage. Only
    #: `user_upload` / `camera_capture` material qualifies — see
    #: `AVATAR_OWNED_SOURCE_TYPES`.
    source_object_keys: tuple[str, ...] = ()
    #: Canonical renders of every deleted version (front + side when present).
    canonical_object_keys: tuple[str, ...] = ()
    #: Referenced-but-not-owned objects, listed so the caller can prove they
    #: were considered and deliberately left alone (an OOTD photo stays in the
    #: user's OOTD history after they delete their avatar).
    retained_object_keys: tuple[str, ...] = field(default=())


class SqlAlchemyAvatarSourceAssetRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, asset: AvatarSourceAsset) -> AvatarSourceAsset:
        self.session.add(asset)
        await self.session.flush()
        return asset

    async def get_for_user(self, asset_id: uuid.UUID, user_id: uuid.UUID) -> AvatarSourceAsset | None:
        return cast(
            AvatarSourceAsset | None,
            await self.session.scalar(
                select(AvatarSourceAsset).where(
                    AvatarSourceAsset.id == asset_id,
                    AvatarSourceAsset.user_id == user_id,
                )
            ),
        )

    async def get_by_object_key(self, object_key: str, user_id: uuid.UUID) -> AvatarSourceAsset | None:
        """The already-known row for a photo, if any.

        V3-012's dedupe read: a photo that has been seen before must not be
        re-offered, whatever the user decided about it (V3_SPEC §7.3). The
        unique constraint makes the write side safe; this makes the read side
        cheap.
        """
        return cast(
            AvatarSourceAsset | None,
            await self.session.scalar(
                select(AvatarSourceAsset).where(
                    AvatarSourceAsset.source_object_key == object_key,
                    AvatarSourceAsset.user_id == user_id,
                )
            ),
        )

    async def has_declined_material(self, user_id: uuid.UUID) -> bool:
        """Has this user ever said "暂不用" to person-reference material?

        V3-012's *cross-asset* suppression read, and the reason it is a query
        rather than a profile column: V3_SPEC §7.3 has two separate rules, and
        `one_row_per_source_object` only satisfies the first. "每张候选只提示
        一次" is per-photo — the unique constraint covers it. "用户选'暂不用'
        后不在日常流程反复提醒" is per-*user*: after one decline, the next
        distinct OOTD photo must not re-open the question, or a user who said
        no once gets asked again tomorrow and the day after, which is exactly
        the 反复提醒 the spec forbids.

        Deliberately a bare EXISTS rather than `list_for_user` + a Python
        filter: this runs on every natural-flow detection and the number of
        source assets per user is unbounded.

        Only `declined` counts. `pending` material is an open question, not a
        refusal, and `accepted` material means the user is already somewhere
        further along the state machine, which the profile-status rules handle.
        """
        return (
            await self.session.scalar(
                select(AvatarSourceAsset.id)
                .where(
                    AvatarSourceAsset.user_id == user_id,
                    AvatarSourceAsset.consent_status == AvatarConsentStatus.DECLINED.value,
                )
                .limit(1)
            )
        ) is not None

    async def list_for_user(self, user_id: uuid.UUID) -> list[AvatarSourceAsset]:
        result = await self.session.scalars(
            select(AvatarSourceAsset)
            .where(AvatarSourceAsset.user_id == user_id)
            .order_by(AvatarSourceAsset.created_at.desc(), AvatarSourceAsset.id)
        )
        return list(result)


class SqlAlchemyAvatarProfileRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_for_user(self, user_id: uuid.UUID) -> AvatarProfile | None:
        return cast(
            AvatarProfile | None,
            await self.session.scalar(select(AvatarProfile).where(AvatarProfile.user_id == user_id)),
        )

    async def add(self, profile: AvatarProfile) -> AvatarProfile:
        self.session.add(profile)
        await self.session.flush()
        return profile

    async def add_version(self, version: AvatarVersion) -> AvatarVersion:
        self.session.add(version)
        await self.session.flush()
        return version

    async def get_version(self, version_id: uuid.UUID, profile_id: uuid.UUID) -> AvatarVersion | None:
        return cast(
            AvatarVersion | None,
            await self.session.scalar(
                select(AvatarVersion).where(
                    AvatarVersion.id == version_id,
                    AvatarVersion.avatar_profile_id == profile_id,
                )
            ),
        )

    async def list_versions(self, profile_id: uuid.UUID) -> list[AvatarVersion]:
        result = await self.session.scalars(
            select(AvatarVersion)
            .where(AvatarVersion.avatar_profile_id == profile_id)
            .order_by(AvatarVersion.created_at.desc(), AvatarVersion.id)
        )
        return list(result)


class AvatarLifecycle:
    """The legal state transitions over a profile, its consent and its versions."""

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def offer_candidate(self, profile: AvatarProfile, asset: AvatarSourceAsset) -> None:
        """`none → candidate` — qualified material now exists (contract §4.1).

        V3-012's only write to the profile, and the reason it is here rather
        than in the detector: every other transition already lives in this
        class, and the one thing this transition must never do is touch
        consent. It doesn't — `candidate` is exactly "qualified material
        exists, **not yet consented**", so the asset stays `pending` and the
        profile's `consent_version` / `consented_at` are not arguments to this
        method at all. Detection cannot grant permission because there is no
        parameter through which it could.

        The three preconditions are checked rather than trusted:

        - the asset must belong to the same user as the profile, or one
          person's photo could be offered on another's profile;
        - it must have passed the quality assessment, so a `rejected` or
          not-yet-assessed photo can never become the thing a user is asked
          about;
        - its consent must still be `pending`, so an already-declined photo
          cannot be re-offered by re-running detection — V3_SPEC §7.3's
          prompt-once rule, enforced instead of remembered.

        Only a profile with no material on offer moves. `candidate` is left
        alone (an offer is already outstanding), and `preparing` / `ready` /
        `refresh_recommended` / `disabled` are deliberately not reachable from
        here — the frozen transition list has no edge from them to
        `candidate`, and inventing one would let a background detection
        interrupt a preparation or reverse a revocation.
        """
        if asset.user_id != profile.user_id:
            raise ValueError("cannot offer a source asset that belongs to a different user")
        if asset.quality_status != AvatarQualityStatus.PASSED.value:
            raise ValueError("only material that passed the quality assessment can be offered")
        if asset.consent_status != AvatarConsentStatus.PENDING.value:
            raise ValueError("cannot offer a source asset whose consent is already decided")
        if profile.status != AvatarStatus.NONE.value:
            return
        profile.status = AvatarStatus.CANDIDATE.value
        await self.session.flush()

    async def record_consent(
        self,
        profile: AvatarProfile,
        asset: AvatarSourceAsset,
        *,
        accept: bool,
        consent_version: str | None = None,
        now: datetime | None = None,
    ) -> None:
        """Apply one consent decision to one source asset (contract §4.2/§4.3).

        Accepting writes the copy version onto **both** the asset (which
        version this specific photo was accepted under — it cannot be rewritten
        by a later copy revision) and the profile (what the frozen GET shape
        returns as `consent`, which must still be answerable once
        `status=ready` leaves `candidate` null and once the photo itself has
        been purged at retention). Writing both here is what keeps them from
        drifting; nothing else may set them.

        Declining is terminal for that asset ("accept=false dismisses that
        candidate permanently") and records nothing profile-level, because no
        consent was given. It does move a `candidate` profile back to `none` —
        the offer is withdrawn — but it never writes `disabled_at` or a consent
        version: refusing one photo is not revoking the avatar.

        **A decided asset is not re-decidable.** "Terminal" has to mean the row
        cannot be re-decided, not merely that the UI stops offering it:
        otherwise a declined photo could be flipped to accepted by a replayed
        request, and re-accepting under newer copy would silently rewrite the
        audit record of what the user was actually shown. An exact repeat is
        treated as an idempotent retry (network retries are normal and must not
        500); anything else raises, which V3-013 maps to the frozen
        `4091 STATE_CONFLICT`.
        """
        decided_at = now or datetime.now(UTC)
        if asset.consent_status != AvatarConsentStatus.PENDING.value:
            already = asset.consent_status == (
                AvatarConsentStatus.ACCEPTED.value if accept else AvatarConsentStatus.DECLINED.value
            )
            if already and asset.consent_version == (consent_version if accept else None):
                return
            raise ValueError(
                "consent for this source asset is already decided and cannot be changed"
            )
        if not accept:
            asset.consent_status = AvatarConsentStatus.DECLINED.value
            asset.consent_decided_at = decided_at
            asset.consent_version = None
            if profile.status == AvatarStatus.CANDIDATE.value:
                # The candidate is gone; the profile falls back to having no
                # material on offer. It does not become `disabled` — declining
                # one photo is not revoking the avatar (contract §4.1).
                profile.status = AvatarStatus.NONE.value
            await self.session.flush()
            return

        if not consent_version:
            raise ValueError("accepting a candidate requires the consent copy version")
        asset.consent_status = AvatarConsentStatus.ACCEPTED.value
        asset.consent_decided_at = decided_at
        asset.consent_version = consent_version
        profile.consent_version = consent_version
        profile.consented_at = decided_at
        # `candidate → preparing` on explicit consent (contract §4.1). Clearing
        # `disabled_at` is what makes `disabled → preparing` ("user re-enables
        # via new consent") a real transition rather than a stuck state.
        #
        # A profile that already HAS a live version keeps its status: the frozen
        # transition list has no `ready → preparing`, deliberately. Consenting
        # to fresh material while ready is the `replace` flow, and moving to
        # `preparing` would force `active_version_id` NULL through the
        # `active_version_matches_status` CHECK — i.e. take the user's working
        # avatar away for the duration of a preparation that might fail. That is
        # exactly the gap contract §4.2 forbids. The new version is prepared
        # alongside and swapped in by `activate_version`.
        if profile.active_version_id is None:
            profile.status = AvatarStatus.PREPARING.value
        profile.disabled_at = None
        await self.session.flush()

    async def activate_version(
        self,
        profile: AvatarProfile,
        version: AvatarVersion,
        *,
        now: datetime | None = None,
    ) -> None:
        """Swap a freshly prepared version in — `preparing → ready`.

        The previous version is retired, never mutated, and the pointer moves
        in the same transaction. That is the whole of contract §4.2's "atomic
        swap on success ... replace failure keeps the previous version active":
        a failed preparation simply never reaches this method, so the old row
        is still live and still pointed at.

        The pairing is checked rather than trusted. No foreign key can express
        "this version belongs to this profile" — `active_version_id` only
        references `avatar_versions.id` — so a caller that mixed up ids while
        finalizing concurrent preparations could point profile A at profile B's
        canonical render, i.e. show one person another person's avatar. It
        would also wedge B: deleting B's version fires ON DELETE SET NULL
        against A's row and trips A's ready-state CHECK. A leak between two
        users' body images is not a defect worth leaving to caller discipline.
        """
        if version.avatar_profile_id != profile.id:
            raise ValueError("cannot activate a version that belongs to a different avatar profile")
        retired_at = now or datetime.now(UTC)
        previous_id = profile.active_version_id
        if previous_id is not None and previous_id != version.id:
            previous = await self.session.get(AvatarVersion, previous_id)
            if previous is not None and previous.retired_at is None:
                previous.retired_at = retired_at
        version.retired_at = None
        profile.active_version_id = version.id
        profile.status = AvatarStatus.READY.value
        profile.disabled_at = None
        await self.session.flush()

    async def disable(self, profile: AvatarProfile, *, now: datetime | None = None) -> None:
        """Revoke: `any → disabled` (contract §4.1).

        Blocks new generation immediately — no active version means nothing can
        be generated from it — while keeping every row. Existing decisions
        stay readable (contract §4.3), the consent record stays auditable, and
        `disabled → preparing` remains reachable through a new consent.

        The live version is retired rather than deleted, so re-enabling is a
        preparation, not a resurrection of material the user switched off.
        """
        disabled_at = now or datetime.now(UTC)
        active_id = profile.active_version_id
        # Clear the pointer BEFORE retiring, so the CHECK sees status and
        # pointer change together.
        profile.active_version_id = None
        profile.status = AvatarStatus.DISABLED.value
        # Idempotent: revoking an already-revoked profile keeps the ORIGINAL
        # timestamp. "When did the user turn this off" must not drift forward
        # every time the disabled path is re-entered — notably `delete_avatar`,
        # which disables first. Re-enabling clears it, so a genuine second
        # revocation still gets its own timestamp.
        if profile.disabled_at is None:
            profile.disabled_at = disabled_at
        if active_id is not None:
            active = await self.session.get(AvatarVersion, active_id)
            if active is not None and active.retired_at is None:
                active.retired_at = disabled_at
        await self.session.flush()

    async def delete_avatar(
        self,
        profile: AvatarProfile,
        *,
        purge_derived: bool,
        now: datetime | None = None,
    ) -> AvatarDeletionPlan:
        """`DELETE /v1/avatar-profile` — "Disable and delete" (contract §4.2).

        Deletion is disable **plus** material removal, in that order:

        1. the profile is disabled (revocation is recorded, not erased — a
           tombstone is how "the user turned this off on <date>" survives, and
           how `disabled → preparing` stays available if they come back);
        2. every version row is deleted, so nothing can be generated from a
           stale reference;
        3. every source-asset row is deleted;
        4. the object keys the avatar **owns** are returned for storage
           deletion, and the ones it merely referenced are returned separately
           as retained.

        The profile row must be disabled before its versions are deleted: the
        `active_version_matches_status` CHECK would otherwise reject the
        implicit `SET NULL` on the pointer, which is the constraint doing its
        job — an active version cannot silently vanish from under a ready
        profile.

        Full row erasure is not this path. It is the account-level PIPL purge
        (`DELETE /v1/me`), where the `users` FK cascade removes profile,
        versions and source assets with no avatar-specific code at all.

        **Consequence V3-012 must inherit**: deleting the source-asset rows
        also deletes the `declined` ones, so the per-row prompt-once guarantee
        (`one_row_per_source_object`) no longer covers those photos. After a
        deletion the suppression is the profile tombstone — a `disabled`
        profile must not be prompted back into a candidate sheet by the daily
        flow (V3_SPEC §7.3: no repeated asking). Keeping the declined rows
        instead would mean a user who asked us to delete their avatar data
        still has a list of their photos on our side, which is worse.
        """
        await self.disable(profile, now=now)

        versions = list(
            await self.session.scalars(
                select(AvatarVersion).where(AvatarVersion.avatar_profile_id == profile.id)
            )
        )
        canonical_keys: list[str] = []
        for version in versions:
            canonical_keys.append(version.canonical_front_object_key)
            if version.canonical_side_object_key:
                canonical_keys.append(version.canonical_side_object_key)

        assets = list(
            await self.session.scalars(
                select(AvatarSourceAsset).where(AvatarSourceAsset.user_id == profile.user_id)
            )
        )
        owned_keys: list[str] = []
        retained_keys: list[str] = []
        for asset in assets:
            target = (
                owned_keys
                if AvatarSourceType(asset.source_type) in AVATAR_OWNED_SOURCE_TYPES
                else retained_keys
            )
            target.append(asset.source_object_key)

        await self.session.execute(delete(AvatarVersion).where(AvatarVersion.avatar_profile_id == profile.id))
        await self.session.execute(
            delete(AvatarSourceAsset).where(AvatarSourceAsset.user_id == profile.user_id)
        )
        await self.session.flush()

        return AvatarDeletionPlan(
            profile_disabled=True,
            purge_derived=purge_derived,
            source_object_keys=tuple(owned_keys),
            canonical_object_keys=tuple(canonical_keys),
            retained_object_keys=tuple(retained_keys),
        )
