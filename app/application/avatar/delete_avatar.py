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

**What `purge_derived` governs, stated plainly.** Contract §4.2 makes it a
required query parameter with no default: "`true` also deletes generated
**try-on assets** derived from any avatar version." Two things follow, and the
two branches are genuinely different:

1. The avatar's own canonical renders (`avatar_versions.canonical_*_object_key`)
   are **not** try-on assets — they are the appearance reference itself, the
   thing V3_SPEC §15 calls 形象参考. They are removed unconditionally by every
   delete, `purge_derived` or not, because a user who deleted their avatar did
   not ask us to keep a generated likeness of themselves. They are derived
   assets and they are removed, which is what satisfies acceptance 3.
2. Generated try-on previews — `outfit_visual_assets` rows with
   `visual_type=static_tryon` and their PNGs under `users/<id>/tryon/static/` —
   are removed **only** when `purge_derived=true`. Rows go in the request
   (`delete_static_tryon_for_user`), objects go to the same `avatar_purge` job
   as the rest. `false` keeps them, which is the frozen contract: the parameter
   has no default precisely so the user decides, and a delete that swept them
   either way would make the parameter decorative.

**This was previously deferred, and the deferral was wrong.** Until V3-017 there
was no writer of `visual_type=static_tryon`, so the earlier version of this
docstring said the scope was empty by construction and pointed at a test
asserting `outfit_visual_assets` has no `avatar_version_id` column. V3-017 made
the premise false without making that test fail — a column-shaped tripwire
cannot see a new *writer* — so `purge_derived` would have shipped inert while
the thing it governs finally existed. The tripwire is replaced by the behavioral
pair in `tests/services/test_v3_avatar_lifecycle_apis.py` (`true` ⇒ no row, no
object, nothing signable; `false` ⇒ the render survives), which cannot pass
without the deletion actually happening.

**A render in flight is covered too.** A row the worker is rendering right now
has `object_key` still NULL, so the sweep names
`static_preview_object_key(user_id, asset_id)` instead — the one key that render
can occupy. `static_tryon_worker` closes the same window from its side by
re-reading the row before it finalizes. Neither depends on the other's timing,
which is the point: a crash between `put_object` and the finalize would defeat
the worker-side check alone.

**What is not solved here.** These renders still have no per-row retention clock
(`outfit_visual_assets` has no `expires_at` column; §13.5's 30-day rule lives in
`retention.yaml` against the `aidrobe-private` bucket and is not verified to
reach this prefix in every storage configuration). A user who never deletes
their avatar keeps them until the account purge. That is a V3-018/V3-019
question about the asset's own lifecycle, not about revocation, and it is
recorded rather than quietly fixed here.

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

import hashlib
import uuid
from collections.abc import Sequence
from dataclasses import dataclass

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.persistence.models.avatar import AvatarProfile
from app.infrastructure.persistence.repositories.avatar_repo import (
    AvatarDeletionPlan,
    AvatarLifecycle,
)
from app.infrastructure.persistence.repositories.outfit_visual_repo import (
    SqlAlchemyOutfitVisualRepository,
)
from app.infrastructure.storage.object_key import static_preview_object_key
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

#: 32 hex chars of SHA-256 over the object keys this deletion covers. A length
#: budget, not secrecy — the composed key is a fixed 82 characters against
#: `idempotency_key`'s String(128), no matter how many objects are swept.
_KEY_SET_DIGEST_CHARS = 32


def purge_idempotency_key(profile_id: uuid.UUID, object_keys: Sequence[str]) -> str:
    """Stable per (profile, exact set of objects being deleted).

    Keyed on the profile **and the key set**, because the profile id alone is
    not unique to one deletion. A deleted profile survives as a `disabled`
    tombstone and keeps its id, so the frozen `disabled → preparing` re-enable
    reuses it: delete, re-enable, upload, prepare, delete again, and the second
    purge composed the same key as the first. `enqueue` is `ON CONFLICT DO
    NOTHING`, so it returned the *succeeded* first job without replacing its
    payload — and the new photo and canonical render were never swept, while the
    response reported them as scheduled. Person material retained after a
    deletion request, reported as deleted: the worst direction for this
    particular bug to point.

    Retry idempotency is preserved rather than traded away, which is why the
    digest is over the key set and not a deletion counter: two DELETEs over the
    *same* objects still compose the same key and still collapse to one sweep,
    including the second call whose plan is empty. Sorted first, because the
    plan's order comes from two separate queries and an unstable order would
    mint a new key for an identical deletion — the same determinism trap V3-005
    hit with its scan ordering.
    """
    digest = hashlib.sha256("\n".join(sorted(object_keys)).encode("utf-8")).hexdigest()[
        :_KEY_SET_DIGEST_CHARS
    ]
    return f"{JOB_TYPE}:{profile_id}:{digest}"


class AvatarProfileNotFound(Exception):
    """No avatar profile for this user — the router answers 404 (contract §3)."""


@dataclass(frozen=True)
class AvatarDeleted:
    plan: AvatarDeletionPlan
    #: None when there was nothing to remove from storage, so a caller can tell
    #: "purge scheduled" from "nothing to purge" without inspecting the queue.
    purge_job_id: uuid.UUID | None
    deleted_object_count: int
    #: How many `static_tryon` rows `purge_derived=true` removed. Always 0 when
    #: the flag was false. Not in any response shape (§4.2 freezes those); it
    #: exists so the deletion's own log can say whether the derived scope was
    #: non-empty without naming a single key.
    deleted_derived_asset_count: int = 0


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

        derived_keys = (
            await self._purge_static_tryon(user_id) if purge_derived else ()
        )
        keys.extend(derived_keys)

        purge_job_id: uuid.UUID | None = None
        if keys:
            job, created = await enqueue(
                self.session,
                job_type=JOB_TYPE,
                # Keyed on the profile AND the exact key set — see
                # `purge_idempotency_key`. A second DELETE over the same objects
                # still collapses into the first sweep; a *later* deletion of a
                # rebuilt avatar gets its own job instead of being swallowed by
                # the completed one, which is what the profile-only key did.
                idempotency_key=purge_idempotency_key(profile.id, keys),
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
            derived_asset_count=len(derived_keys),
        )
        return AvatarDeleted(
            plan=plan,
            purge_job_id=purge_job_id,
            deleted_object_count=len(keys),
            deleted_derived_asset_count=len(derived_keys),
        )

    async def _purge_static_tryon(self, user_id: uuid.UUID) -> tuple[str, ...]:
        """Delete the try-on renders and return the objects to sweep.

        One key per deleted row, and never zero: a row whose render has not
        landed yet has no `object_key`, but the worker can only ever write it to
        `static_preview_object_key(user_id, asset_id)`, so that key is named
        anyway. `delete_objects` treats a missing key as success, so naming a
        render that never happened costs nothing — while *not* naming it would
        leave a PNG written moments after this transaction commits.
        """
        rows = await SqlAlchemyOutfitVisualRepository(
            self.session
        ).delete_static_tryon_for_user(user_id)
        return tuple(
            key or static_preview_object_key(user_id, asset_id) for asset_id, key in rows
        )
