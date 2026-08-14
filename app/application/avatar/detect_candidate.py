"""V3-012 — notice reusable person material in flows the user is already in.

The whole feature in one sentence: when a user uploads a full-body photo for
some *other* reason (an OOTD record, an outfit import), look at it, and if it
could serve as a person reference, record that it could — and stop there.

**Detection cannot grant consent** (acceptance 1). Structurally, not by
discipline: this module never assigns to a consent field, `offer_candidate` has
no consent parameter, and a new `avatar_source_assets` row is born
`consent_status='pending'` from the column default. The only permission-shaped
thing that happens is `none → candidate`, whose contract §4.1 meaning is
literally "qualified material exists, **not yet consented**".

**Nothing here may break the flow it rides on** (acceptance 2). Detection runs
after the host request has answered, in
`app/infrastructure/workers/avatar_candidate_worker.py`, on a session of its
own. It cannot poison the host transaction, cannot delay the response and
cannot fail it — the strongest form of "non-blocking" available, rather than a
try/except around a write that would already have aborted the caller's
transaction. The host request's only contribution is an `async_jobs` row, and
`enqueue_candidate_detection` writes even that inside a SAVEPOINT it swallows,
so the *enqueue* cannot fail the flow either.

This module therefore takes a resolved `object_key`, never a URL: the
URL→key step belongs to the enqueue site, which is the last place a signed URL
legitimately exists. Keeping it there means no signed URL is written into
`async_jobs.payload` (contract §7) and the job's idempotency key is stable
across two differently-signed URLs for the same photo — without which
prompt-once would leak one duplicate offer per re-signing.

**Prompt-once, and its one exception** (acceptance 4). BE cannot observe a
prompt — that is FE's surface (V3-014) — so V3-012's enforceable proxy is that
material is *offered* at most once, which is the same caveat shape V3-003 used
for its flag-off criterion and V3-005 for its "API" one.

V3_SPEC §7.3 states two rules, and they need two different mechanisms:

1. *每张候选只提示一次* — **per photo**. `(user_id, source_object_key)` is
   unique, so re-seeing a photo cannot mint a second row whatever the user
   decided about the first; the pre-read short-circuits before any segmentation
   runs, and `offer_candidate` independently refuses an asset whose consent is
   already decided.
2. *用户选"暂不用"后不在日常流程反复提醒* — **per user**, and NOT implied by
   (1). One declined photo does nothing to the *next* distinct OOTD photo, so
   per-asset uniqueness alone would let a user who said no once be asked again
   tomorrow, and the day after — the 反复提醒 the spec forbids, failing in the
   product-harmful direction. `SkipReason.DECLINED_BY_USER` is that rule:
   a natural flow stays quiet once `has_declined_material` is true.

Rule 2 is deliberately independent of what V3-013 decides a decline should do
to `profile.status`, which is not yet settled. Whether a decline leaves the
profile at `candidate`, resets it to `none` or moves it to `disabled`, natural
flows go quiet either way — so this cannot regress on a choice made in another
feature. Profile status still carries its own suppression:
`preparing`/`ready`/`refresh_recommended` mean the user has an avatar (a new
one is the `replace` flow, which only they may start) and `disabled` is the
revocation tombstone `AvatarLifecycle.delete_avatar` documented as its
successor's problem.

The exception is the second half of acceptance 4, "**unless the user later
requests preview**" (V3_SPEC §7.3: 未来仅在用户主动要求上身预演时重新说明).
`CandidateDetectionTrigger.PREVIEW_REQUEST` is that path: when the user is the
one asking for a try-on, a revoked profile may be offered *newly seen* material
again. It still cannot re-offer a photo that already has a row, and it still
cannot decide consent. No caller passes it yet — V3-013's upload endpoint and
V3-015's on-demand photo setup are its users — but the rule lives here, with
the rest of the suppression logic, rather than being deferred into a feature
that would have to re-derive it.

**The flag gates detection itself, which is a deliberate deviation.** The repo
convention (BE CLAUDE.md) is "persist regardless of the flag, gate exposure",
and contract §2 rule 3 freezes that data written while a flag was on survives
the flag going off. Both are honored: nothing is deleted when
`ENABLE_V3_AVATAR` flips off, and rows written while it was on stay readable.
What does not happen with the flag off is the *work* — no person photo is
fetched, no segmentation runs, no row is written. The convention exists to make
rollback lossless, and gating detection loses nothing, because an assessment is
recomputable from the photo at any time; the alternative would mean running
human segmentation over every OOTD upload and persisting person-material
records for a feature that is switched off.
"""

import uuid
from dataclasses import dataclass
from enum import StrEnum

import structlog
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.avatar.candidate_rules import assess_person_frame
from app.application.ports.avatar_detection import PersonFrameDetectorPort
from app.application.ports.storage import ObjectStoragePort
from app.common.enums import AvatarQualityStatus, AvatarSourceType, AvatarStatus
from app.config.settings import Settings
from app.infrastructure.persistence.models.avatar import AvatarProfile, AvatarSourceAsset
from app.infrastructure.persistence.repositories.avatar_repo import (
    AvatarLifecycle,
    SqlAlchemyAvatarProfileRepository,
    SqlAlchemyAvatarSourceAssetRepository,
)

logger = structlog.get_logger(__name__)


class CandidateDetectionTrigger(StrEnum):
    """Why detection is running — which decides how suppression applies."""

    #: The user was doing something else (OOTD record, outfit import). Never
    #: re-opens a question the user has already closed.
    NATURAL_FLOW = "natural_flow"
    #: The user actively asked for a try-on preview. May offer newly seen
    #: material even to a profile the user previously switched off.
    PREVIEW_REQUEST = "preview_request"


class CandidateDetectionOutcome(StrEnum):
    OFFERED = "offered"
    #: Assessed and not usable. The row is kept so the same photo is never
    #: re-examined, and so V3-013 can explain why nothing was offered.
    NOT_USABLE = "not_usable"
    #: Could not be assessed (no detector, unreadable bytes). Left `pending`.
    UNDETERMINED = "undetermined"
    #: Never looked at. See `SkipReason`.
    SKIPPED = "skipped"


class SkipReason(StrEnum):
    FEATURE_DISABLED = "feature_disabled"
    #: This exact photo already has a row — whatever its verdict or consent.
    ALREADY_ASSESSED = "already_assessed"
    #: The user already declined person-reference material once. V3_SPEC §7.3's
    #: per-user rule; see the module docstring.
    DECLINED_BY_USER = "declined_by_user"
    #: An offer is already outstanding for this user.
    CANDIDATE_ALREADY_OFFERED = "candidate_already_offered"
    #: Preparing / ready / refresh_recommended — the user has an avatar; a new
    #: one is the `replace` flow, which only they may start.
    AVATAR_ALREADY_ESTABLISHED = "avatar_already_established"
    #: The user switched the avatar off. Natural flows never ask again.
    REVOKED_BY_USER = "revoked_by_user"
    #: The bytes could not be read from storage.
    IMAGE_UNAVAILABLE = "image_unavailable"


#: Profile states in which a natural flow must stay quiet.
_ESTABLISHED_STATUSES = frozenset(
    {
        AvatarStatus.PREPARING.value,
        AvatarStatus.READY.value,
        AvatarStatus.REFRESH_RECOMMENDED.value,
    }
)


@dataclass(frozen=True)
class CandidateDetectionResult:
    """What one detection run did. Carries no image reference by design —
    it is returned to a caller that may log it (contract §7 forbids object
    keys, signed URLs and body inferences in logs and analytics)."""

    outcome: CandidateDetectionOutcome
    reason: SkipReason | None = None
    source_asset_id: uuid.UUID | None = None
    quality_status: str | None = None
    profile_status: str | None = None


class DetectAvatarCandidate:
    def __init__(
        self,
        session: AsyncSession,
        *,
        storage: ObjectStoragePort,
        detector: PersonFrameDetectorPort,
        settings: Settings,
    ) -> None:
        self.session = session
        self.storage = storage
        self.detector = detector
        self.settings = settings
        self.assets = SqlAlchemyAvatarSourceAssetRepository(session)
        self.profiles = SqlAlchemyAvatarProfileRepository(session)
        self.lifecycle = AvatarLifecycle(session)

    async def execute(
        self,
        *,
        user_id: uuid.UUID,
        source_type: AvatarSourceType,
        object_key: str,
        trigger: CandidateDetectionTrigger = CandidateDetectionTrigger.NATURAL_FLOW,
    ) -> CandidateDetectionResult:
        if not self.settings.ENABLE_V3_AVATAR:
            return _skip(SkipReason.FEATURE_DISABLED)

        if await self.assets.get_by_object_key(object_key, user_id) is not None:
            return _skip(SkipReason.ALREADY_ASSESSED)

        profile = await self.profiles.get_for_user(user_id)
        suppressed = _suppression(profile, trigger)
        if suppressed is not None:
            return _skip(suppressed)

        # Ordered after the profile rules on purpose: those are free (the
        # profile is already loaded) and cover the common cases, so the one
        # extra statement is only spent when they all pass. Ordered *before*
        # reading the photo, so a suppressed flow never pays segmentation.
        if trigger is CandidateDetectionTrigger.NATURAL_FLOW and await self.assets.has_declined_material(
            user_id
        ):
            return _skip(SkipReason.DECLINED_BY_USER)

        observation = await self._observe(object_key)
        if observation is _UNREADABLE:
            return _skip(SkipReason.IMAGE_UNAVAILABLE)

        assessment = assess_person_frame(observation)  # type: ignore[arg-type]
        asset = await self._record(
            user_id=user_id,
            source_type=source_type,
            object_key=object_key,
            quality_status=assessment.quality_status,
            signals=assessment.signals,
        )
        if asset is None:
            # Lost the race against a concurrent detection of the same photo.
            # The other writer's row is the one row this photo gets.
            return _skip(SkipReason.ALREADY_ASSESSED)

        if not assessment.is_offerable:
            return CandidateDetectionResult(
                outcome=(
                    CandidateDetectionOutcome.NOT_USABLE
                    if assessment.quality_status is AvatarQualityStatus.REJECTED
                    else CandidateDetectionOutcome.UNDETERMINED
                ),
                source_asset_id=asset.id,
                quality_status=asset.quality_status,
                profile_status=profile.status if profile else None,
            )

        if profile is None:
            profile = await self.profiles.add(AvatarProfile(user_id=user_id))
        await self.lifecycle.offer_candidate(profile, asset)
        return CandidateDetectionResult(
            outcome=CandidateDetectionOutcome.OFFERED,
            source_asset_id=asset.id,
            quality_status=asset.quality_status,
            profile_status=profile.status,
        )

    async def _observe(self, object_key: str) -> object:
        """Read the bytes and measure them. `_UNREADABLE` when storage failed,
        `None` when the detector could not judge — different outcomes, because
        an unreadable object is worth retrying on the next natural flow and an
        unavailable detector is worth recording as examined-but-undetermined."""
        try:
            image_bytes = await self.storage.get_object(object_key)
        except Exception:
            # No key, no URL, no bytes in the log line (contract §7).
            logger.warning("avatar.candidate_detection.image_unavailable")
            return _UNREADABLE
        try:
            return await self.detector.observe(image_bytes)
        except Exception:
            logger.warning("avatar.candidate_detection.detector_unavailable")
            return None

    async def _record(
        self,
        *,
        user_id: uuid.UUID,
        source_type: AvatarSourceType,
        object_key: str,
        quality_status: AvatarQualityStatus,
        signals: dict[str, object],
    ) -> AvatarSourceAsset | None:
        """Insert the one row this photo gets, or None if another writer won.

        `one_row_per_source_object` closes the race; a SAVEPOINT keeps the
        violation from poisoning the session, the V3-007 precedent. No
        check-then-insert guard — a 23505 here is a concurrent duplicate, not
        a defect.
        """
        asset = AvatarSourceAsset(
            user_id=user_id,
            source_type=source_type.value,
            source_object_key=object_key,
            quality_status=quality_status.value,
            quality_signals=signals,
        )
        try:
            async with self.session.begin_nested():
                self.session.add(asset)
                await self.session.flush()
        except IntegrityError:
            return None
        return asset


def _suppression(
    profile: AvatarProfile | None, trigger: CandidateDetectionTrigger
) -> SkipReason | None:
    if profile is None:
        return None
    if profile.status in _ESTABLISHED_STATUSES:
        return SkipReason.AVATAR_ALREADY_ESTABLISHED
    if profile.status == AvatarStatus.CANDIDATE.value:
        return SkipReason.CANDIDATE_ALREADY_OFFERED
    if profile.status == AvatarStatus.DISABLED.value:
        # The one place the trigger changes the answer. A user who asked for a
        # preview is asking us to raise this again; a daily flow is not.
        if trigger is CandidateDetectionTrigger.PREVIEW_REQUEST:
            return None
        return SkipReason.REVOKED_BY_USER
    return None


def _skip(reason: SkipReason) -> CandidateDetectionResult:
    return CandidateDetectionResult(outcome=CandidateDetectionOutcome.SKIPPED, reason=reason)


class _Unreadable:
    """Sentinel distinct from `None`; see `DetectAvatarCandidate._observe`."""


_UNREADABLE = _Unreadable()
