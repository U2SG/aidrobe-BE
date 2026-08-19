"""V3-013 — the six frozen avatar surfaces (contract §4.2).

Read the state, answer one consent prompt, register a deliberately uploaded
photo, start a preparation, replace a version, delete everything.

Gated by `ENABLE_V3_AVATAR`, default off → 503 (contract §2 rollback rule 1, the
`ENABLE_SENSE_CHECK` / `ENABLE_V3_PURCHASE_DECISION` precedent). Rows persist
regardless of the flag, so flipping it back is a clean rollback with no data
loss, and turning it off hides the avatar completely — including for users who
already consented.

**Where the work is not.** No endpoint here calls a provider. Consent and delete
are row transitions; prepare and replace insert one job row each. Everything
expensive runs in `avatar_preparation_worker` / `avatar_purge_worker`. That is
what keeps a person's photo out of the request path and lets the 503 gate be a
pure exposure gate.

**Transitions are not re-derived here.** Every state change goes through
`AvatarLifecycle` (V3-011), which owns the frozen §4.1 edges and the CHECK
constraints behind them. This module's job is HTTP: gate the flag, resolve the
caller, translate one closed vocabulary of refusals into the frozen error codes,
and sign URLs at read time.

**Ownership before anything else.** `POST /v1/avatar-profile/uploads` takes a
client-supplied storage reference, which is the exact shape of the cross-user
read V3-012's round-3 review found on `ootd_image_url`: the backend would
otherwise fetch whatever key it resolved to using *its own* credentials, bypassing
the access control on the URL the caller actually held. `is_owned_object_key` —
the shared predicate, not a second copy — gates it, and the refusal is `404` per
contract §3 ("Resources that exist but are not owned by the caller return
NOT_FOUND"), so the response cannot be used to probe which keys exist. This
surface answers 404 rather than V3-012's silent no-op because it is a deliberate
user action, not a host flow that must succeed regardless.
"""

import uuid
from typing import Annotated, cast

import structlog
from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.avatar.delete_avatar import AvatarProfileNotFound, DeleteAvatarProfile
from app.application.avatar.detect_candidate import CandidateDetectionTrigger
from app.application.avatar.enqueue_candidate_detection import enqueue_candidate_detection
from app.application.avatar.prepare_avatar import (
    PreparationMode,
    PreparationRefusal,
    PreparationRefused,
    PrepareAvatar,
)
from app.common import error_codes
from app.common.analytics_emit import emit_v3_event
from app.common.analytics_events import V3AnalyticsEventName
from app.common.enums import AvatarConsentStatus, AvatarSourceType, AvatarStatus
from app.common.exceptions import FriendlyException
from app.config.settings import get_settings
from app.infrastructure.persistence.models.avatar import (
    AvatarProfile,
    AvatarSourceAsset,
    AvatarVersion,
)
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.repositories.avatar_repo import AvatarLifecycle
from app.infrastructure.persistence.session import get_db_session
from app.infrastructure.storage.object_key import (
    extract_internal_object_key,
    is_owned_object_key,
)
from app.presentation.api.v1.deps import get_current_user
from app.presentation.api.v1.render_url import resolve_render_url
from app.presentation.api.v1.schemas.avatar import (
    AvatarActiveVersionModel,
    AvatarCandidateModel,
    AvatarConsentModel,
    AvatarConsentRequest,
    AvatarConsentResponse,
    AvatarDeleteResponse,
    AvatarPrepareResponse,
    AvatarProfileResponse,
    AvatarUploadRegistrationRequest,
    AvatarUploadRegistrationResponse,
)

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/v1", tags=["avatar"])


def _gate() -> None:
    if not get_settings().ENABLE_V3_AVATAR:
        raise FriendlyException(
            error_codes.FORBIDDEN,
            "V3 avatar is not enabled",
            status_code=503,
        )


#: `prepare` / `replace` refusals → the frozen §3 error codes. A mapping rather
#: than raises scattered through the use case, so every refusal is visibly
#: assigned exactly one code and a new refusal cannot silently default to 500.
_PREPARE_ERRORS: dict[PreparationRefusal, tuple[int, str, int]] = {
    PreparationRefusal.NO_PROFILE: (
        error_codes.NOT_FOUND,
        "no avatar profile",
        404,
    ),
    PreparationRefusal.NO_CONSENTED_SOURCE: (
        error_codes.STATE_CONFLICT,
        "no consented avatar source material",
        409,
    ),
    PreparationRefusal.ALREADY_PREPARING: (
        # Contract §4.2: "prepare/replace while a preparation job is running ⇒
        # 4091 STATE_CONFLICT".
        error_codes.STATE_CONFLICT,
        "an avatar preparation is already running",
        409,
    ),
    PreparationRefusal.WRONG_PROFILE_STATE: (
        error_codes.STATE_CONFLICT,
        "avatar profile is not in a state that allows this preparation",
        409,
    ),
}


def _flow_id() -> str | None:
    """The request's correlation id, when it is one a funnel can join on.

    `v3_avatar_consent` is `when_in_flow`, not `required` — a consent reached
    outside a preview flow is still a consent and must still be counted, so
    unlike V3-008's `required` emitter this returns None rather than suppressing
    the event. A fabricated id would corrupt every funnel join, so a
    non-UUID correlation id yields no flow rather than a made-up one.
    """
    correlation_id = structlog.contextvars.get_contextvars().get("correlation_id")
    try:
        return str(uuid.UUID(str(correlation_id)))
    except (TypeError, ValueError, AttributeError):
        return None


async def _load_profile(session: AsyncSession, user_id: uuid.UUID) -> AvatarProfile | None:
    return cast(
        AvatarProfile | None,
        await session.scalar(select(AvatarProfile).where(AvatarProfile.user_id == user_id)),
    )


async def _render_profile(
    session: AsyncSession, user_id: uuid.UUID, profile: AvatarProfile | None
) -> AvatarProfileResponse:
    """Build the frozen §4.2 shape.

    `status=none` is a normal 200 for a user who has never had a profile — the
    contract says so explicitly, and a 404 here would make "no avatar yet"
    indistinguishable from an error on FE's optional, self-hiding entry.

    `active_version` and `consent` are populated strictly by status, matching the
    frozen comments (`active_version` unless ready/refresh_recommended, `consent`
    until first consent). That is the same rule the
    `active_version_matches_status` CHECK enforces in the database, so the
    response cannot disagree with the row.

    **`candidate` is populated by the material, not by the status (V3-021).** It
    used to require `status == candidate`, and that hid two frozen paths from
    every client:

    - §4.1's `disabled → preparing`, "user re-enables via new consent". A
      `PREVIEW_REQUEST` detection deliberately assesses a new photo for a
      revoked profile and deliberately leaves the status at `disabled`
      (`detect_candidate`: there is no `disabled → candidate` edge, and moving
      would erase the revocation tombstone). The material qualified, its id was
      known, and nothing could read it — so the frozen re-enable edge had no
      path and V3-015 shipped a `revoked` dead end that said so.
    - §4.2's `replace` from a *new* photo. `record_consent` keeps a profile with
      a live version at `ready` on purpose, so the working avatar is not taken
      away for a preparation that might fail; the same blindness applied.

    So the rule is now "is there material awaiting your decision", which is what
    the block has always *meant* — `_candidate_asset` has only ever matched
    `quality_status=passed` + `consent_status=pending`, i.e. exactly that. Two
    consequences, both deliberate:

    - **`preparing` still reports null.** §4.2's conflict rule refuses a second
      preparation, so a consent taken there could not be acted on, and an offer
      the user cannot use is worse than no offer.
    - **The cost, stated rather than waved away.** Every non-`preparing` profile
      read now costs one extra indexed lookup on `avatar_source_assets`, where
      before only a `candidate` profile paid it — and the profile GET *is* polled
      (V3-015's setup screen, every 1.5s for up to 10 attempts, precisely while
      the status is `none` or `disabled`). That is the query this widening exists
      to answer, so it is the poll doing its job rather than overhead the
      exclusion of `preparing` could avoid.
    - **Widening what is *reported* is not widening what is *prompted*.** The
      passive V3-014 sheet gates on `status === 'candidate'`
      (`isPromptableCandidate`), so a `ready` or `disabled` profile is still
      never nagged — V3_SPEC §7.3. Only the surfaces the user themselves opened
      read this block.
    """
    if profile is None:
        return AvatarProfileResponse(status=AvatarStatus.NONE.value)

    active: AvatarActiveVersionModel | None = None
    if profile.active_version_id is not None:
        version = await session.get(AvatarVersion, profile.active_version_id)
        if version is not None:
            active = AvatarActiveVersionModel(
                id=str(version.id),
                created_at=version.created_at,
                # Signed at read time from the stable key; nothing TTL-bound is
                # ever persisted (V3_SPEC §13.5).
                canonical_front_url=resolve_render_url(version.canonical_front_object_key, None),
            )

    candidate: AvatarCandidateModel | None = None
    if profile.status != AvatarStatus.PREPARING.value:
        asset = await _candidate_asset(session, user_id)
        if asset is not None:
            candidate = AvatarCandidateModel(
                source_asset_id=str(asset.id),
                source_type=asset.source_type,
                preview_url=resolve_render_url(asset.source_object_key, None),
            )

    consent: AvatarConsentModel | None = None
    if profile.consent_version is not None and profile.consented_at is not None:
        consent = AvatarConsentModel(
            version=profile.consent_version, consented_at=profile.consented_at
        )

    return AvatarProfileResponse(
        status=profile.status, active_version=active, candidate=candidate, consent=consent
    )


async def _candidate_asset(session: AsyncSession, user_id: uuid.UUID) -> AvatarSourceAsset | None:
    """The material awaiting this user's consent decision, whatever the status.

    Newest passed-and-undecided asset. Ordered by `created_at, id` so a user with
    two qualified photos is always shown the same one — an offer that changed
    between two GETs would let a client consent to a different photo than the one
    it displayed.

    The predicate is unchanged by V3-021: `passed` + `pending` was always the
    definition of "awaiting a decision". Only the caller's status guard moved.
    """
    from app.common.enums import AvatarConsentStatus, AvatarQualityStatus

    return cast(
        AvatarSourceAsset | None,
        await session.scalar(
            select(AvatarSourceAsset)
            .where(
                AvatarSourceAsset.user_id == user_id,
                AvatarSourceAsset.quality_status == AvatarQualityStatus.PASSED.value,
                AvatarSourceAsset.consent_status == AvatarConsentStatus.PENDING.value,
            )
            .order_by(AvatarSourceAsset.created_at.desc(), AvatarSourceAsset.id.desc())
            .limit(1)
        ),
    )


# Advertised in OpenAPI so generated clients can model the flag-off state every
# operation reaches at runtime (contract §2 rollback rule 1; the FE self-hides
# on it). Description only — the body is the service-wide envelope, which no
# router in this repo re-declares per route (purchase_candidates precedent).
_FLAG_OFF: dict[int | str, dict[str, str]] = {
    503: {"description": "`ENABLE_V3_AVATAR` is off."},
}


@router.get("/avatar-profile", response_model=AvatarProfileResponse, responses=_FLAG_OFF)
async def get_avatar_profile(
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> AvatarProfileResponse:
    _gate()
    profile = await _load_profile(session, user.id)
    return await _render_profile(session, user.id, profile)


@router.post(
    "/avatar-candidates/{source_asset_id}/consent",
    response_model=AvatarConsentResponse,
    responses=_FLAG_OFF,
)
async def decide_avatar_consent(
    source_asset_id: uuid.UUID,
    payload: AvatarConsentRequest,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> AvatarConsentResponse:
    """Accept or decline one candidate (contract §4.2).

    Acceptance 1 lands here: `record_consent` writes the copy version and the
    decision timestamp onto the asset *and* the profile, and the table's CHECK
    constraints refuse a decided consent with no timestamp or an accepted one
    with no version — so a recorded consent cannot be missing either field.

    The two shape rules are enforced before the transition, as business errors
    rather than FastAPI 422s: accepting requires a copy version, and declining
    must not carry one (a "declined under version X" record would imply the user
    agreed to something).
    """
    _gate()
    if payload.accept and not payload.consent_version:
        raise FriendlyException(
            error_codes.VALIDATION_ERROR,
            "accepting a candidate requires the consent copy version",
        )
    if not payload.accept and payload.consent_version:
        raise FriendlyException(
            error_codes.VALIDATION_ERROR,
            "declining a candidate must not carry a consent version",
        )

    asset = await session.scalar(
        select(AvatarSourceAsset).where(
            AvatarSourceAsset.id == source_asset_id,
            AvatarSourceAsset.user_id == user.id,
        )
    )
    if asset is None:
        # Someone else's asset id, or none at all — same answer either way, so
        # the response cannot be used to discover another user's material.
        raise FriendlyException(error_codes.NOT_FOUND, "avatar candidate not found", status_code=404)

    profile = await _load_profile(session, user.id)
    if profile is None:
        # Detection creates the profile alongside the material, so this means the
        # asset predates a deletion that removed the profile. Nothing to consent
        # against.
        raise FriendlyException(error_codes.NOT_FOUND, "no avatar profile", status_code=404)

    # Read before the decision is applied: `record_consent` treats an exact
    # repeat as an idempotent retry and returns quietly, so afterwards a network
    # retry is indistinguishable from a fresh decision. Counting both would
    # inflate the consent rate with duplicates of one user action.
    was_undecided = asset.consent_status == AvatarConsentStatus.PENDING.value
    try:
        await AvatarLifecycle(session).record_consent(
            profile,
            asset,
            accept=payload.accept,
            consent_version=payload.consent_version,
        )
    except ValueError as exc:
        # `record_consent` refuses to re-decide a decided asset — an exact repeat
        # returns quietly, anything else raises. Mapped to the frozen
        # `4091 STATE_CONFLICT` (contract §4.2's conflict rules).
        raise FriendlyException(
            error_codes.STATE_CONFLICT, str(exc), status_code=409
        ) from exc

    logger.info(
        "avatar.consent.recorded",
        accept=payload.accept,
        profile_status=profile.status,
        source_type=asset.source_type,
    )
    if was_undecided:
        # V3-013 owns the backend avatar emitters (analytics contract §0:
        # "emitters ship with the feature that owns the surface ... V3-013
        # avatar"). A decline is emitted too — `accepted` is a field of the
        # event, not a filter on it, and the consent rate needs both halves.
        emit_v3_event(
            V3AnalyticsEventName.AVATAR_CONSENT.value,
            {"accepted": payload.accept, "source_type": asset.source_type},
            user_id=user.id,
            flow_id=_flow_id(),
        )
    return AvatarConsentResponse(
        status=profile.status,
        source_asset_id=str(asset.id),
        consent_status=asset.consent_status,
        consent_version=asset.consent_version,
        consented_at=asset.consent_decided_at,
    )


@router.post(
    "/avatar-profile/uploads",
    response_model=AvatarUploadRegistrationResponse,
    responses=_FLAG_OFF,
)
async def register_avatar_upload(
    payload: AvatarUploadRegistrationRequest,
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> AvatarUploadRegistrationResponse:
    """Source C — a photo the user deliberately took or picked for this purpose.

    Registration is not consent. This hands the photo to V3-012's detection with
    `PREVIEW_REQUEST`, which assesses it and — if it qualifies — offers it as a
    `candidate`; the user then accepts through the consent endpoint. Two steps,
    because V3_SPEC §7.1's source-B rule is that material never becomes a person
    reference implicitly, and a deliberate upload does not change who has to say
    yes. It also means this endpoint cannot be used to skip the consent record
    that acceptance 1 requires.

    `PREVIEW_REQUEST` is the trigger V3-012 built for exactly this caller: it
    widens which *users* may be asked (a `disabled` profile can be offered
    material again, contract §4.1's `disabled → preparing` path) without widening
    which *photos* may be re-offered — a previously declined photo stays declined.
    Until this endpoint existed, that trigger had no caller, which V3-012
    recorded as an open risk.
    """
    _gate()
    settings = get_settings()

    object_key = payload.object_key or extract_internal_object_key(payload.file_url, settings)
    if not object_key:
        raise FriendlyException(
            error_codes.VALIDATION_ERROR,
            "object_key or a resolvable file_url is required",
        )
    if not is_owned_object_key(object_key, user.id):
        # A key this caller was never issued. 404, not 403 — see the module
        # docstring and contract §3.
        logger.warning("avatar.upload_registration.foreign_object_key", user_id=str(user.id))
        raise FriendlyException(error_codes.NOT_FOUND, "upload not found", status_code=404)

    # The **stable key**, passed straight through. An earlier version signed it
    # into a URL just so `extract_internal_object_key` could undo the signing,
    # and that round-trip could not fail loudly: `resolve_render_url` swallows a
    # signing error and returns its fallback (`None` here), so a storage
    # misconfiguration became `NO_INTERNAL_OBJECT_KEY` and a per-purpose-bucket
    # config became `UNRESOLVABLE_STORAGE_BUCKET` — both HTTP 200, both silently
    # dropping a photo the user deliberately took for this. The OOTD and import
    # hooks still pass a URL because a URL is what their host request holds;
    # this surface holds the key already.
    outcome = await enqueue_candidate_detection(
        session,
        user_id=user.id,
        source_type=AvatarSourceType.USER_UPLOAD,
        object_key=object_key,
        settings=settings,
        trigger=CandidateDetectionTrigger.PREVIEW_REQUEST,
    )

    profile = await _load_profile(session, user.id)
    return AvatarUploadRegistrationResponse(
        detection_outcome=outcome.value,
        status=profile.status if profile is not None else AvatarStatus.NONE.value,
    )


@router.post("/avatar-profile/prepare", response_model=AvatarPrepareResponse, responses=_FLAG_OFF)
async def prepare_avatar_profile(
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> AvatarPrepareResponse:
    """Start preparation from a consented source (contract §4.2)."""
    return await _start_preparation(session, user, PreparationMode.PREPARE)


@router.post("/avatar-profile/replace", response_model=AvatarPrepareResponse, responses=_FLAG_OFF)
async def replace_avatar_profile(
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> AvatarPrepareResponse:
    """Prepare a new version; atomic swap on success (contract §4.2).

    Same machinery as `prepare`, different precondition — and the swap itself is
    `AvatarLifecycle.activate_version`, reached only after the provider
    succeeded, which is what makes "replace failure keeps the previous version
    active" true by construction rather than by a compensating write.
    """
    return await _start_preparation(session, user, PreparationMode.REPLACE)


async def _start_preparation(
    session: AsyncSession, user: User, mode: PreparationMode
) -> AvatarPrepareResponse:
    _gate()
    try:
        started = await PrepareAvatar(session, settings=get_settings()).execute(
            user_id=user.id, mode=mode
        )
    except PreparationRefused as exc:
        code, message, status_code = _PREPARE_ERRORS[exc.reason]
        raise FriendlyException(code, message, status_code=status_code) from exc

    return AvatarPrepareResponse(
        status=started.profile_status,
        job_id=str(started.job_id),
        source_asset_id=str(started.source_asset_id),
        created=started.created,
    )


@router.delete("/avatar-profile", response_model=AvatarDeleteResponse, responses=_FLAG_OFF)
async def delete_avatar_profile(
    purge_derived: Annotated[bool, Query()],
    user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> AvatarDeleteResponse:
    """Disable and delete (contract §4.2).

    `purge_derived` is a **required** query parameter with no default — declared
    here without one, so omitting it is a 422 rather than a silent choice about
    somebody's photos. V3_SPEC §13.5 requires being explicit about whether
    derived assets are deleted too, which means the caller has to say.

    What it governs today, and what it does not yet, is documented in
    `app/application/avatar/delete_avatar.py`.
    """
    _gate()
    try:
        deleted = await DeleteAvatarProfile(session).execute(
            user_id=user.id, purge_derived=purge_derived
        )
    except AvatarProfileNotFound as exc:
        raise FriendlyException(
            error_codes.NOT_FOUND, "no avatar profile", status_code=404
        ) from exc

    # `not_applicable`, so no flow_id may be passed — deletion is a settings
    # action, not a step in a preview funnel, and `validate_v3_event` rejects
    # one outright. A repeated DELETE emits again on purpose: the second call is
    # a real user request to delete, answered honestly with nothing left to
    # remove, and suppressing it would hide a user deleting twice because they
    # did not believe us the first time.
    emit_v3_event(
        V3AnalyticsEventName.AVATAR_DELETED.value,
        {"purge_derived": deleted.plan.purge_derived},
        user_id=user.id,
    )

    return AvatarDeleteResponse(
        status=AvatarStatus.DISABLED.value,
        purge_derived=deleted.plan.purge_derived,
        scheduled_object_count=deleted.deleted_object_count,
        purge_job_id=str(deleted.purge_job_id) if deleted.purge_job_id else None,
    )
