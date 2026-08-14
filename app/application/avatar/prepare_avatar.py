"""V3-013 — start a preparation, without ever starting two.

This is V3-013 acceptance 2 ("Prepare is idempotent and does not duplicate paid
work") on the request side. The endpoint does no provider work at all: it
validates that a preparation is legal right now and inserts one `async_jobs`
row. `avatar_preparation_worker` does the paid part later, on its own session.

**Two independent guards, because one of them is not enough.**

1. **The in-flight conflict.** Contract §4.2 freezes it: "prepare/replace while
   a preparation job is running ⇒ `4091 STATE_CONFLICT`". That is a *read* of
   the job table, so it is subject to a race — two requests can both read "no
   job running" before either inserts.
2. **The idempotency key.** `job_runtime.enqueue` is idempotent on
   `idempotency_key` (a unique index), so the loser of that race gets the
   existing row rather than a second one. This is the guard that actually holds
   under concurrency; the 409 is what makes a *sequential* duplicate legible to
   the client instead of looking like fresh work was started.

Neither is decorative and they fail in opposite directions, which is why both
are here. A test covers each ordering: sequential-while-running gets the 409,
and a duplicate that slips past the read collides on the key and charges once.

**What the key is made of, and why.** Everything that determines the paid work:
the profile, the exact source asset, and the provider pipeline identity
(provider + model + prompt version). Same photo through the same pipeline ⇒ same
job ⇒ one charge, forever — re-posting `prepare` after a success is free and
returns the existing job. A *different* consented photo (the `replace` flow) is a
different key, so replacing is never swallowed as a duplicate of the original
preparation. And bumping the pipeline version deliberately re-opens the work,
the same versioning move as V3-012's `assessor` key component and the `/today`
payload cache bump to `p3`.

**Where the provider identity comes from.** `preparation_pipeline_identity()`
mirrors `container.avatar_preparation()` exactly, so the key names the pipeline
that will actually answer the job. V3-012 learned this the hard way with
`assessor_identity`: a key that claims an assessment the worker did not make
makes rows permanently unreachable. A unit test pins the two selectors together.

**Retrying a preparation that died, and why the key alone made that impossible.**
An idempotency key that never expires is exactly right for work that *succeeded*
and exactly wrong for work that died: the same photo through the same pipeline
composes the same key, so once the job reached `dead` every later `prepare`
collided with the terminal row and answered HTTP 200 with a `job_id` that would
never run again. Recovery needed a new photograph. Two layers had to change,
because either one alone still dead-ends:

1. **The status guard.** A first-time failure leaves the profile `candidate`
   (`AvatarLifecycle.fail_preparation`), which the PREPARE whitelist refused —
   so the retry was rejected before it ever reached the key, and with the wrong
   reason ("no consented source" when a consented source was sitting right
   there). `candidate` is now prepare-able; the genuine no-consent case still
   refuses with the same code, one line further down, because the source lookup
   is what answers it.
2. **The terminal row.** `enqueue` is `ON CONFLICT DO NOTHING`, so it hands back
   whatever row owns the key regardless of its status. `_revive_if_terminal`
   re-opens that row — and only that row, and only from `dead`/`failed` — under
   a conditional UPDATE, so two concurrent retries still produce one live job.

The decision recorded in `docs/async-jobs.md`: **revive the row rather than add
an attempt component to the key.** A generation counter would grow the key per
retry and scatter one photo's history across rows; reviving keeps one row per
(profile, photo, pipeline), which is where the cost columns and the error
history already live. Acceptance 2 is untouched by this, and the reason is the
narrow definition of what gets revived: `succeeded` is not a terminal *failure*,
so a repeated `prepare` after a success still returns the existing job and buys
nothing. Only a job that produced no avatar at all re-opens, and only when a
user explicitly asks again.

This also un-sticks a case that has no rollback at all: `recover_expired_leases`
dead-letters a crash-looping job directly, without ever calling `mark_failed`, so
`fail_preparation` never fires and the profile stays `preparing` forever. That
status is in the PREPARE whitelist and a `dead` job is not "in flight", so the
same retry path recovers it.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, cast

import structlog
from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import AvatarConsentStatus, AvatarStatus
from app.config.settings import Settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.avatar import AvatarProfile, AvatarSourceAsset
from app.infrastructure.persistence.repositories.avatar_repo import AvatarLifecycle
from app.infrastructure.workers.job_runtime import (
    JOB_STATUS_DEAD,
    JOB_STATUS_FAILED,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    enqueue,
)

logger = structlog.get_logger(__name__)

JOB_TYPE = "avatar_preparation"

#: Three attempts (the runtime default). Unlike `avatar_candidate`, where a lost
#: job leaves nothing user-visible, a lost preparation leaves the user staring at
#: `preparing` forever — a visible stuck state with a real user waiting on it. A
#: transient storage read or provider blip is worth retrying; the idempotency key
#: means a retry re-runs the *same* job rather than charging for a new one.
MAX_ATTEMPTS = 3

#: 24 hex chars of SHA-256 over the source-asset id — a length budget, not
#: secrecy (the id is already a uuid).
_DIGEST_CHARS = 24

#: 16 hex chars of SHA-256 over the provider pipeline identity.
#:
#: The identity is **digested rather than embedded**, and that is not cosmetic: a
#: realistic vendor triple (`minimax-video-01:<dated-model>:tryon-prompt-v12`)
#: pushed the composed key to 140 characters against `idempotency_key`'s
#: String(128), which would be a 500 on the first `prepare` after V3-016 swaps the
#: adapter — found by `test_a_long_provider_triple_still_fits`, which exists
#: precisely because the fake adapter's short name would have hidden it. Digesting
#: makes the key a **fixed 97 characters** no matter what a provider is called, so
#: the bound cannot be re-broken by naming.
#:
#: What is given up is reading the pipeline out of the key. That is why
#: `pipeline_identity` is carried verbatim in the job payload — operators get the
#: readable value from the row, and the key keeps its budget.
_IDENTITY_DIGEST_CHARS = 16


class PreparationMode(StrEnum):
    """Which of the two frozen endpoints asked for this preparation.

    Same machinery, different precondition — see `_assert_mode_allowed`. The
    mode is carried into the log line (not the key: `replace` and `prepare` over
    the *same* source photo must not be able to charge twice).
    """

    PREPARE = "prepare"
    REPLACE = "replace"


class PreparationRefusal(StrEnum):
    """Why a preparation could not start. The router maps each to a frozen code."""

    NO_PROFILE = "no_profile"
    #: Nothing has been consented to, so there is no material to prepare from.
    #: `prepare` on a fresh profile — the FE must run consent first.
    NO_CONSENTED_SOURCE = "no_consented_source"
    #: Contract §4.2's conflict rule: a preparation is already in flight.
    ALREADY_PREPARING = "already_preparing"
    #: `replace` needs something to replace; `prepare` needs the absence of one.
    WRONG_PROFILE_STATE = "wrong_profile_state"


class PreparationRefused(Exception):
    """Raised so the router can translate one closed vocabulary into one code."""

    def __init__(self, reason: PreparationRefusal) -> None:
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True)
class PreparationStarted:
    job_id: uuid.UUID
    source_asset_id: uuid.UUID
    #: Did THIS request start work? True for a fresh insert and for a retry that
    #: re-opened a dead job; False when an identical preparation had already been
    #: paid for and nothing new was started.
    #:
    #: Deliberately not "a row was inserted". A revived job is not a new row, but
    #: reporting it as `created=False` would tell the client the same thing as
    #: "your duplicate was swallowed" — the precise misreport that let a dead job
    #: masquerade as live work. What a caller can act on is whether something is
    #: running now.
    created: bool
    profile_status: str


def preparation_pipeline_identity(settings: Settings) -> str:
    """The provider pipeline a job queued *now* would be answered by.

    Mirrors `container.avatar_preparation()`. Bounded to a short slug because it
    goes into a length-budgeted key, and because `provider` is internal (contract
    §6.4) — it must never reach a response shape.
    """
    from app.infrastructure.vision.fake_avatar_preparation import (
        MODEL_VERSION,
        PROMPT_VERSION,
        PROVIDER,
    )

    # One adapter exists in P0 and selecting a real provider is a human gate, so
    # this reads the fake pipeline's own constants rather than inventing a
    # settings-driven branch that has nothing to branch to. When V3-016 adds a
    # second adapter this becomes the selector, and the key changes with it —
    # which is the correct behavior, not a migration problem.
    del settings
    return f"{PROVIDER}:{MODEL_VERSION}:{PROMPT_VERSION}"


def preparation_idempotency_key(
    profile_id: uuid.UUID, source_asset_id: uuid.UUID, pipeline_identity: str
) -> str:
    """Stable per (profile, source photo, provider pipeline). Fixed length."""
    digest = hashlib.sha256(str(source_asset_id).encode("utf-8")).hexdigest()[:_DIGEST_CHARS]
    identity_digest = hashlib.sha256(pipeline_identity.encode("utf-8")).hexdigest()[
        :_IDENTITY_DIGEST_CHARS
    ]
    return f"{JOB_TYPE}:{profile_id}:{digest}:{identity_digest}"


class PrepareAvatar:
    """`POST /v1/avatar-profile/prepare` and `.../replace` (contract §4.2)."""

    def __init__(self, session: AsyncSession, *, settings: Settings) -> None:
        self.session = session
        self.settings = settings

    async def execute(
        self,
        *,
        user_id: uuid.UUID,
        mode: PreparationMode,
        correlation_id: str | None = None,
    ) -> PreparationStarted:
        profile = await self.session.scalar(
            select(AvatarProfile).where(AvatarProfile.user_id == user_id)
        )
        if profile is None:
            raise PreparationRefused(PreparationRefusal.NO_PROFILE)

        self._assert_mode_allowed(profile, mode)

        source = await self._latest_consented_source(user_id)
        if source is None:
            raise PreparationRefused(PreparationRefusal.NO_CONSENTED_SOURCE)

        if await self._preparation_in_flight(profile.id):
            raise PreparationRefused(PreparationRefusal.ALREADY_PREPARING)

        pipeline_identity = preparation_pipeline_identity(self.settings)
        job, created = await enqueue(
            self.session,
            job_type=JOB_TYPE,
            idempotency_key=preparation_idempotency_key(
                profile.id, source.id, pipeline_identity
            ),
            payload={
                "user_id": str(user_id),
                "avatar_profile_id": str(profile.id),
                "source_asset_id": str(source.id),
                # Readable here because the key only carries its digest — see
                # `_IDENTITY_DIGEST_CHARS`. Internal (contract §6.4): it is on the
                # job row, never in a response.
                "pipeline_identity": pipeline_identity,
                # The stable object key, never a signed URL (contract §7) — the
                # worker reads storage by key, and `async_jobs.payload` outlives
                # any log line.
                "source_object_key": source.source_object_key,
                "mode": mode.value,
            },
            user_id=user_id,
            target_id=profile.id,
            correlation_id=correlation_id,
            max_attempts=MAX_ATTEMPTS,
        )

        revived = False
        if not created:
            revived = await self._revive_if_terminal(job)
        started = created or revived

        # Only a preparation that is actually going to run may move the profile.
        # A swallowed duplicate must not, or a `prepare` that started nothing
        # would still reset the state the previous attempt left behind.
        resumed = False
        if started:
            resumed = await AvatarLifecycle(self.session).begin_preparation(profile)

        # Closed vocabularies only: no object key, no URL, no person detail.
        logger.info(
            "avatar.preparation.enqueued",
            mode=mode.value,
            created=created,
            revived=revived,
            resumed=resumed,
            profile_status=profile.status,
        )
        return PreparationStarted(
            job_id=job.id,
            source_asset_id=source.id,
            created=started,
            profile_status=profile.status,
        )

    async def _revive_if_terminal(self, job: AsyncJob) -> bool:
        """Re-open a preparation that died, so a retry is not a silent no-op.

        Guarded three ways, because this is the one place that moves a job
        *backwards* out of a terminal state:

        - **Only a terminal failure.** `succeeded` is excluded, which is what
          keeps acceptance 2 true: a repeated `prepare` after a success still
          returns the existing job and buys nothing.
        - **Only under a conditional UPDATE.** The `status IN (dead, failed)`
          predicate is re-evaluated by Postgres under the row lock, so of two
          concurrent retries exactly one gets `rowcount == 1` and the other is
          told plainly that it started nothing. Reading the status in Python and
          then writing would be the check-then-act race this avoids.
        - **`attempts` resets, `last_error` does not.** The retry is user-
          initiated, so it gets the full attempt budget rather than inheriting an
          exhausted one; the previous failure's error stays on the row until the
          next attempt overwrites it, because it is the only record of why the
          user is retrying at all.
        """
        if job.status not in (JOB_STATUS_DEAD, JOB_STATUS_FAILED):
            return False
        now = datetime.now(UTC)
        result = cast(
            CursorResult[Any],
            await self.session.execute(
                update(AsyncJob)
                .where(
                    AsyncJob.id == job.id,
                    AsyncJob.status.in_((JOB_STATUS_DEAD, JOB_STATUS_FAILED)),
                )
                .values(
                    status=JOB_STATUS_QUEUED,
                    attempts=0,
                    next_run_at=now,
                    lease_expires_at=None,
                    updated_at=now,
                )
            ),
        )
        if result.rowcount != 1:
            return False
        # The row was updated behind the ORM's back; without this the identity
        # map keeps serving the terminal status to anything that reads `job`
        # later in this request.
        await self.session.refresh(job)
        logger.info("avatar.preparation.revived", job_id=str(job.id))
        return True

    @staticmethod
    def _assert_mode_allowed(profile: AvatarProfile, mode: PreparationMode) -> None:
        """The one real difference between the two endpoints.

        `replace` means "prepare a new version; atomic swap on success", so it
        requires a version to swap *out* — and that is precisely the state in
        which `record_consent` deliberately leaves the profile `ready` rather
        than moving it to `preparing`, so the user's working avatar is not taken
        away for the duration of a preparation that might fail (contract §4.2).

        `prepare` is the first-time path: consent has moved the profile to
        `preparing` and there is no active version yet. Calling it on a `ready`
        profile is a client error rather than a silent second preparation —
        `replace` is the endpoint for that, and conflating them would make the
        atomic-swap guarantee depend on which URL the client happened to pick.

        `disabled` is prepare-able: contract §4.1 lists `disabled → preparing`
        ("user re-enables via new consent"), and a fresh consent is exactly what
        `record_consent` clears `disabled_at` for.

        **`candidate` is prepare-able too, and that is the retry path.** It is
        the state `fail_preparation` leaves behind — "the profile showing
        `candidate` again is what makes a retry available" — so refusing it here
        made that promise false: the retry never reached the enqueue. It is also
        the state a user sits in with a *fresh* offer they have not consented to,
        and that case must still be refused. The two are told apart by evidence
        rather than by status: whether a consented source actually exists, which
        the `source is None` check immediately below answers with this same
        `NO_CONSENTED_SOURCE` reason. Deciding it there rather than here is what
        stops a genuine retry being refused with "no consented avatar source
        material" while the consented photo is on file.
        """
        has_active_version = profile.active_version_id is not None
        if mode is PreparationMode.REPLACE and not has_active_version:
            raise PreparationRefused(PreparationRefusal.WRONG_PROFILE_STATE)
        if mode is PreparationMode.PREPARE and has_active_version:
            raise PreparationRefused(PreparationRefusal.WRONG_PROFILE_STATE)
        if mode is PreparationMode.PREPARE and profile.status not in (
            AvatarStatus.PREPARING.value,
            AvatarStatus.DISABLED.value,
            AvatarStatus.CANDIDATE.value,
        ):
            # `none` means there is no material at all. Saying so as a distinct
            # refusal keeps "you skipped consent" from being reported as "you
            # already have an avatar".
            raise PreparationRefused(PreparationRefusal.NO_CONSENTED_SOURCE)

    async def _latest_consented_source(self, user_id: uuid.UUID) -> AvatarSourceAsset | None:
        """The most recently accepted source asset.

        Ordered by the consent decision, not by row creation: a user who
        consents to an older photo after a newer one should get the photo they
        just said yes to. `id` breaks ties so two decisions inside the same
        transaction timestamp still resolve deterministically — without it the
        idempotency key could differ between two runs over identical data, which
        is the same determinism trap V3-005 hit with its scan ordering.
        """
        return cast(
            AvatarSourceAsset | None,
            await self.session.scalar(
                select(AvatarSourceAsset)
                .where(
                    AvatarSourceAsset.user_id == user_id,
                    AvatarSourceAsset.consent_status == AvatarConsentStatus.ACCEPTED.value,
                )
                .order_by(
                    AvatarSourceAsset.consent_decided_at.desc(),
                    AvatarSourceAsset.id.desc(),
                )
                .limit(1)
            ),
        )

    async def _preparation_in_flight(self, profile_id: uuid.UUID) -> bool:
        """Is a preparation for this profile queued or running?

        `target_id` carries the profile, so this is one indexed-ish read rather
        than a payload scan. Terminal states (`succeeded` / `failed` / `dead`)
        are deliberately not in flight: a failed preparation must be retryable,
        and a succeeded one is what `replace` supersedes.
        """
        found = await self.session.scalar(
            select(AsyncJob.id)
            .where(
                AsyncJob.job_type == JOB_TYPE,
                AsyncJob.target_id == profile_id,
                AsyncJob.status.in_((JOB_STATUS_QUEUED, JOB_STATUS_RUNNING)),
            )
            .limit(1)
        )
        return found is not None
