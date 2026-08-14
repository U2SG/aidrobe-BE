"""V3-013 — `DELETE /v1/avatar-profile`, and what happens to the images.

V3-013 acceptance 3: "Delete removes or schedules removal of derived assets
according to policy." Both halves are here, and they are different halves:

- **Rows go in the request.** `AvatarLifecycle.delete_avatar` (V3-011) disables
  the profile, deletes every version row and every source-asset row, and returns
  an `AvatarDeletionPlan` describing the objects. After this commits, nothing can
  be generated from a stale reference — which is the part that must not wait.
- **Objects go out of band.** Object deletion is neither transactional nor
  reversible, so it must not run inside the request transaction: a rollback after
  a successful `delete_objects` would leave the rows intact and the images
  destroyed. The plan's keys are handed to an `avatar_purge` job instead, which
  is the "schedules removal" half — and being a job, it retries, which matters
  because a lost purge leaks person material rather than merely losing a render.

**What `purge_derived` actually governs today, stated plainly.** Contract §4.2
makes it a required query parameter with no default: "`true` also deletes
generated **try-on assets** derived from any avatar version." Two things follow,
and the second is a declared limitation rather than a quiet gap:

1. The avatar's own canonical renders (`avatar_versions.canonical_*_object_key`)
   are **not** try-on assets — they are the appearance reference itself, the
   thing V3_SPEC §15 calls 形象参考. They are removed unconditionally by every
   delete, `purge_derived` or not, because a user who deleted their avatar did
   not ask us to keep a generated likeness of themselves. They are derived
   assets and they are removed, which is what satisfies acceptance 3.
2. Generated try-on previews cannot exist yet. `POST /v1/wardrobe-decisions/{id}/
   visual-preview` is not implemented (V3-016/V3-017), no row is written with
   `visual_type=static_tryon`, and `outfit_visual_assets` has no
   `avatar_version_id` to join on — V3-011's `AvatarDeletionPlan` docstring
   already assigned that column to V3-018. So `purge_derived` is recorded on the
   purge job and carried into the response, and its try-on scope activates when
   there is something in scope. `test_purge_derived_scope_is_empty_by_construction`
   asserts the premise (no avatar link exists on that table) rather than
   asserting the behavior, so the day V3-018 adds the column the test fails and
   points a reader straight at this docstring instead of leaving a parameter that
   silently does nothing.

Deliberately **not** reinterpreted: an earlier draft of this feature had
`purge_derived=false` retain the canonical renders on the
`AVATAR_RETIRED_VERSION_RETENTION_DAYS` clock, so that the two branches would
visibly differ and acceptance 3 would have a two-sided test. That reading was
dropped because it contradicts the frozen §4.2 sentence — it would make the base
delete keep a generated likeness the user asked to remove, in order to
manufacture a testable branch. Fitting the requirement to the implementation is
the failure mode CLAUDE.md names; the limitation above is the honest alternative.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.persistence.models.avatar import AvatarProfile
from app.infrastructure.persistence.repositories.avatar_repo import (
    AvatarDeletionPlan,
    AvatarLifecycle,
)
from app.infrastructure.workers.job_runtime import enqueue

logger = structlog.get_logger(__name__)

JOB_TYPE = "avatar_purge"

#: The runtime default. A dropped purge leaves a person's photos and canonical
#: renders in the bucket after they asked for deletion, so this is the one avatar
#: job type where retries are the point — the opposite trade-off from
#: `avatar_candidate` (`MAX_ATTEMPTS=1`, where a lost job leaves nothing behind).
#: `delete_objects` is documented idempotent, so a retry after a partial success
#: is safe by construction.
MAX_ATTEMPTS = 3


class AvatarProfileNotFound(Exception):
    """No avatar profile for this user — the router answers 404 (contract §3)."""


@dataclass(frozen=True)
class AvatarDeleted:
    plan: AvatarDeletionPlan
    #: None when there was nothing to remove from storage, so a caller can tell
    #: "purge scheduled" from "nothing to purge" without inspecting the queue.
    purge_job_id: uuid.UUID | None
    deleted_object_count: int


class DeleteAvatarProfile:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def execute(
        self,
        *,
        user_id: uuid.UUID,
        purge_derived: bool,
        correlation_id: str | None = None,
    ) -> AvatarDeleted:
        profile = await self.session.scalar(
            select(AvatarProfile).where(AvatarProfile.user_id == user_id)
        )
        if profile is None:
            raise AvatarProfileNotFound

        plan = await AvatarLifecycle(self.session).delete_avatar(
            profile, purge_derived=purge_derived
        )

        # Only what the avatar owns. `plan.retained_object_keys` is deliberately
        # excluded: an OOTD photo stays in the user's own OOTD history after they
        # delete their avatar — deleting it here would destroy unrelated user
        # content because it once happened to be a candidate.
        keys = list(plan.source_object_keys) + list(plan.canonical_object_keys)

        purge_job_id: uuid.UUID | None = None
        if keys:
            job, created = await enqueue(
                self.session,
                job_type=JOB_TYPE,
                # Keyed on the profile, not on the key list: a second DELETE
                # while the first purge is still queued must not enqueue a second
                # sweep over the same objects. The profile id is stable across
                # both calls and the second call's plan is empty anyway (the rows
                # are already gone), so this is belt-and-braces rather than the
                # load-bearing guard.
                idempotency_key=f"{JOB_TYPE}:{profile.id}",
                payload={
                    "user_id": str(user_id),
                    "avatar_profile_id": str(profile.id),
                    # Stable object keys, never signed URLs (contract §7).
                    "object_keys": keys,
                    "purge_derived": purge_derived,
                },
                user_id=user_id,
                target_id=profile.id,
                correlation_id=correlation_id,
                max_attempts=MAX_ATTEMPTS,
            )
            purge_job_id = job.id
            if not created:
                logger.info("avatar.purge.already_queued", object_count=len(keys))

        # Counts and the flag only — no keys, no URLs (contract §7).
        logger.info(
            "avatar.profile.deleted",
            purge_derived=purge_derived,
            owned_object_count=len(keys),
            retained_object_count=len(plan.retained_object_keys),
        )
        return AvatarDeleted(
            plan=plan,
            purge_job_id=purge_job_id,
            deleted_object_count=len(keys),
        )
