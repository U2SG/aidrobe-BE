"""V3-017 — ask for a static try-on without ever paying for the same one twice.

This is the request side of contract §6.1's
`POST /v1/wardrobe-decisions/{id}/visual-preview`. It calls no provider: it
validates that a preview is legal and renderable right now, resolves the frozen
§6.3 cache key, and either hands back the asset that key already names or
inserts one `outfit_visual_assets` row plus one `async_jobs` row.
`static_tryon_worker` does the paid part later, on its own session.

V3-017 registers **no router** — V3-019 owns the endpoint, its response schema
and the OpenAPI change. What this module exposes instead is one closed refusal
vocabulary (`PreviewRefusal`) so that mapping is a translation rather than a
second set of rules; the suggested codes are named at each member.

---

**THREE GUARDS AGAINST A DUPLICATE PAID JOB, and why one is not enough.**

1. **The cache read.** The common case is not a race at all: the user taps
   "see it on me" twice, or comes back tomorrow. A read on the unique
   `cache_key` answers that for free.
2. **The unique index** (`uq_outfit_visual_assets_cache_key`, migration 0049).
   The read races — two requests can both find nothing and both insert — and
   Postgres is what resolves it. The loser catches `23505` and reads the winner
   back. No check-then-insert guard: a `23505` here is a race, not a bug, and
   `decide_purchase` set the SAVEPOINT precedent for containing it.
3. **The job idempotency key.** `job_runtime.enqueue` is idempotent on
   `idempotency_key`, and §6.3 derives that key from the same digest, so the
   asset and its job are 1:1 by construction rather than by convention.

**QUOTA: the DB-aggregate path, deliberately, and this diverges from a sibling.**

`ootd_posters.py:82` passes `redis_client` to `assert_within_quota`; this does
not, and the reason is acceptance 3 ("Failed calls do not burn call quota
according to established cost-guard rules"). The Redis path is an *irrevocable
reservation*: `INCRBY` at admission, rolled back only when the guard itself
refuses. Nothing refunds it when the work later fails, and nothing refunds it
when the admission turns out not to create work at all. Under Redis both a
dead-lettered render and a cache hit would permanently consume a unit of a
5-per-day budget — which contradicts acceptance 3 *and* `cost_guard`'s own
documented rule for call-count quotas ("failed retries don't burn quota").

The DB aggregate has no side effect: it COUNTs `async_jobs` rows in
`(succeeded, running)`, so a `dead` job counts zero and an admission that ends
in a lost race costs nothing. That is not a weaker guarantee here, it is the
rule the criterion names.

**What is given up, corrected.** An earlier version of this docstring said
over-admission "needs N distinct decisions raced by one user inside one
aggregate read". That was wrong, and understated the gap: `queued` is not one of
the counted statuses, and it is a *persistent* state, not a momentary one. A job
sits `queued` until a drain tick claims it (`MAX_DRAIN_PER_TICK=5`, cron-paced),
so sequential requests for distinct decisions issued faster than the drain each
count zero against `TRYON_DAILY_CALL_LIMIT` and are each admitted. No
concurrency is required. This is `cost_guard`'s documented DB-path behaviour and
the same shape as `cutout`'s, and it conforms to acceptance 3 — but the bound is
"the drain keeps up", not "a read window is narrow", and only the second of
those is something the request path controls.

**Two open cost items for the production human gate**, recorded here because
neither is an acceptance failure and neither should be discovered later:

1. The over-admission above. Closing it means either an atomic reservation that
   *refunds* on cache hit and on dead-letter (which Redis's `INCRBY` path does
   not do today), or counting `queued` and accepting that a lost enqueue race
   leaks a unit.
2. A user-initiated revive re-opens a dead job and buys up to `MAX_ATTEMPTS`
   more provider calls, and a dead job counts zero — so a persistently failing
   render can be retried without bound by pressing the button. Charging the
   revive (which `_revive` does) meters *the press*, not the retries behind it.
   A per-(user, cache_key) revive cap is the fix; it is not implemented because
   no acceptance criterion reaches it and the flag is off.

Both gate turning `ENABLE_V3_STATIC_TRYON` on in production with a real,
paid provider — which is a human gate regardless (V3-016).

`ootd_poster` is not changed to match: it is another feature's module, its quota
is a different bucket, and its trade-off was made with different criteria.

**THE FROZEN REQUEST BODY IS NOT CONSUMED HERE, and V3-019 owns that.**
Contract §6.1 freezes the `POST …/visual-preview` body as
`{"view_type": "static_tryon", "concern": "volume|color|length|overall",
"consent": true}`. `execute()` takes none of the three: V3-017 builds the
pipeline, not the route, and there is no router or request schema in this
feature at all (`openapi-unchanged.log` shows the artifact does not move).

Written down rather than left implicit because two of the three are not
decoration. `consent` is a per-request confirmation that this render of the
user's likeness was asked for — it has no persisted counterpart anywhere in this
module, so whoever adds the route must decide where it lands and must not treat
the avatar-level consent (V3-013) as covering it. `concern` is the §6.1 enum
(`volume|color|length|overall`), which contract §5.3 keeps *distinct* from the
decision-level `concern` enum — V3-001's evaluator flagged that pair explicitly
as two frozen enums that must not be unified. `view_type` is single-valued in
P0, so this module hard-codes `VIEW_TYPE` into the cache key; a second view type
makes it a request field and changes the key's components, not just its inputs.

**Quota is checked AFTER the cache read and BEFORE the enqueue.** The order is
load-bearing rather than incidental: checking first would refuse to show a user
their *already generated* preview once they hit the daily cap, which spends
nothing and shows nothing.

**THE LINK-ONLY CANDIDATE, which is refused rather than attempted.** A
`share_link` / `ask_link` candidate stores `source_url` and no bytes anywhere —
`purchase_candidates` has a CHECK admitting exactly one source reference and no
normalized-image column, so there is no garment image to render and no key to
hash. Such a request is refused before the quota check and before any row is
written, so a preview that could only ever fail never costs a call, never
creates an asset the user watches spin, and never enqueues a job. See the module
docstring of `static_tryon_worker` for the other half of that rule.

Stated as a carried risk rather than fixed here: `purchase_decision_rules
._visual_need` does not look at whether the candidate has an image, so a
link-sourced candidate with a `volume` concern is still told `visual_need =
recommended`. That is V3-007's, and the unblock is one of two — gate
`_visual_need` on candidate-image presence, or have V3-019 surface this refusal
as a "no preview possible" state rather than an error. This module refuses
honestly either way; it cannot make the offer upstream stop appearing.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, cast

import structlog
from sqlalchemy import CursorResult, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.cost_guard import assert_within_quota
from app.application.ports.static_tryon import GarmentSlot, PipelineIdentity, StaticTryOnPort
from app.application.purchase.outfit_composition import CATEGORY_SLOTS
from app.application.purchase.static_preview_cache_key import (
    StaticPreviewCacheKey,
    build_cache_key,
)
from app.common.enums import AVATAR_STATUSES_WITH_ACTIVE_VERSION, VisualType
from app.config.settings import Settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.avatar import AvatarProfile, AvatarVersion
from app.infrastructure.persistence.models.outfit_visual import OutfitVisualAsset
from app.infrastructure.persistence.models.purchase_decision import (
    PurchaseCandidate,
    WardrobeDecision,
)
from app.infrastructure.workers.job_runtime import (
    JOB_STATUS_DEAD,
    JOB_STATUS_FAILED,
    JOB_STATUS_QUEUED,
    enqueue,
)

logger = structlog.get_logger(__name__)

#: The async-job type, and therefore the **quota bucket**. `tryon` is the name
#: `cost_guard._limit_for` already maps to `TRYON_DAILY_CALL_LIMIT` and that
#: `GET /v1/user/cost-summary` already publishes a `tryon` field for — a field
#: that has reported 0 since Phase E3 because nothing wrote the job type. V3-017
#: is what makes it live, with no settings change and no response-shape change.
#:
#: It differs from the `static_tryon:` idempotency-key prefix on purpose, and
#: that is the one place this job type breaks the repo's prefix-matches-job_type
#: habit: contract §6.3 freezes the key string as
#: `static_tryon:{sha256(cache_key)}`, while the quota bucket is a pre-existing
#: name this feature does not get to rename. Recorded in `docs/async-jobs.md`.
JOB_TYPE = "tryon"

#: Contract §6.1's frozen `view_type`, and the `visual_type` written on the row.
VIEW_TYPE = VisualType.STATIC_TRYON.value

#: Three attempts, the runtime default and V3-013's choice for the same reason:
#: a lost preview leaves a real user watching a spinner, so a transient storage
#: read or provider blip is worth retrying. The idempotency key means a retry
#: re-runs the *same* job rather than buying a new one, and `usage_today`'s
#: call-count mode counts the row once no matter how many attempts it took.
#:
#: This bounds *technical* retries. Contract §6.3's separate "at most one model
#: retry after a quality-gate failure" is a different budget over a different
#: signal, and it lands with the gate that produces that signal (V3-018).
MAX_ATTEMPTS = 3

#: The generation_status a fresh row starts in (contract §6.2).
STATUS_QUEUED = "queued"


class PreviewRefusal(StrEnum):
    """Why a preview could not be requested. V3-019 maps each to one code."""

    #: Flag off ⇒ contract §2 rollback rule 1: FORBIDDEN / 503.
    FEATURE_DISABLED = "feature_disabled"
    #: Missing, or owned by someone else. Contract §3: NOT_FOUND either way, so
    #: a probe cannot learn that a decision id exists.
    DECISION_NOT_FOUND = "decision_not_found"
    #: Contract §6.1, verbatim: avatar not `ready` / `refresh_recommended` ⇒
    #: `4091 STATE_CONFLICT`.
    AVATAR_NOT_READY = "avatar_not_ready"
    #: The candidate is a link: no garment image exists to render. Suggested
    #: `4091 STATE_CONFLICT` — the decision exists, its state does not permit a
    #: preview — but see the module docstring: the honest FE treatment is a
    #: "no preview possible" state, not an error.
    NO_RENDERABLE_CANDIDATE = "no_renderable_candidate"
    #: The decision composed no grounded outfit, so there is nothing to put on
    #: the avatar beside the candidate (V3_SPEC §5.4 layer 4). V3-007 already
    #: reports `visual_need = none` for this case, so reaching it means the
    #: client ignored that.
    NO_GROUNDED_OUTFIT = "no_grounded_outfit"


class PreviewRefused(Exception):
    """Raised so one closed vocabulary becomes one code at the router."""

    def __init__(self, reason: PreviewRefusal) -> None:
        super().__init__(reason.value)
        self.reason = reason


@dataclass(frozen=True)
class PreviewRequested:
    asset_id: uuid.UUID
    generation_status: str
    #: Did THIS request start paid work? False for a cache hit — contract §6.1's
    #: "cache hit ⇒ 200 with the existing asset". True for a fresh enqueue and
    #: for a retry that re-opened a dead job.
    #:
    #: Deliberately not "a row was inserted", the distinction V3-013's
    #: `PreparationStarted.created` records: a revived job is not a new row, but
    #: reporting it as a cache hit would tell the client its retry was swallowed.
    created: bool


@dataclass(frozen=True)
class _RenderInputs:
    """Everything the key and the payload need, resolved once."""

    candidate_object_key: str
    candidate_slot: str
    garments: tuple[tuple[uuid.UUID, str], ...]
    avatar_version_id: uuid.UUID


class RequestStaticPreview:
    """`POST /v1/wardrobe-decisions/{id}/visual-preview` (contract §6.1)."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        settings: Settings,
        static_tryon: StaticTryOnPort,
    ) -> None:
        self.session = session
        self.settings = settings
        # Injected rather than resolved from the container here, so the
        # application layer depends only on the port — V3-016 acceptance 1,
        # which `test_v3_static_tryon_port.py` asserts over the import graph.
        self.static_tryon = static_tryon

    async def execute(
        self,
        *,
        user_id: uuid.UUID,
        decision_id: uuid.UUID,
        correlation_id: str | None = None,
    ) -> PreviewRequested:
        if not self.settings.ENABLE_V3_STATIC_TRYON:
            # First, before anything is read. Flag off means no row, no quota
            # consumed and no job — contract §2 rollback rule 1.
            raise PreviewRefused(PreviewRefusal.FEATURE_DISABLED)

        decision = await self._owned_decision(user_id, decision_id)
        inputs = await self._resolve_inputs(user_id, decision)
        identity = self.static_tryon.pipeline_identity
        key = build_cache_key(
            avatar_version_id=inputs.avatar_version_id,
            source_object_key=inputs.candidate_object_key,
            supporting_item_ids=[item_id for item_id, _slot in inputs.garments],
            view_type=VIEW_TYPE,
            provider=identity.provider,
            model_version=identity.model_version,
            prompt_version=identity.prompt_version,
        )

        cached = await self._cached_asset(key)
        if cached is not None:
            return await self._serve_cached(cached, user_id=user_id, correlation_id=correlation_id)

        # Only now, with a cache miss confirmed, does this cost anything. Raises
        # FriendlyException(429); see the module docstring for why no
        # `redis_client` is passed.
        await assert_within_quota(self.session, user_id, JOB_TYPE)
        return await self._insert_or_replay(
            key,
            inputs,
            identity,
            user_id=user_id,
            decision_id=decision_id,
            correlation_id=correlation_id,
        )

    # --- Reads ------------------------------------------------------------

    async def _owned_decision(
        self, user_id: uuid.UUID, decision_id: uuid.UUID
    ) -> WardrobeDecision:
        decision = await self.session.scalar(
            select(WardrobeDecision).where(
                WardrobeDecision.id == decision_id,
                WardrobeDecision.user_id == user_id,
            )
        )
        if decision is None:
            # Ownership is in the WHERE rather than in a Python comparison after
            # the fetch, so "not yours" and "does not exist" are one query and
            # one answer (contract §3, no existence leak).
            raise PreviewRefused(PreviewRefusal.DECISION_NOT_FOUND)
        return decision

    async def _resolve_inputs(
        self, user_id: uuid.UUID, decision: WardrobeDecision
    ) -> _RenderInputs:
        """Resolve the avatar, the candidate garment and the supporting items.

        Ordered cheapest-refusal-first so a request that cannot possibly render
        does the least work: the avatar state is one indexed read and refuses
        the largest class of callers (anyone who has not built an avatar).
        """
        avatar_version_id = await self._active_avatar_version(user_id)
        if avatar_version_id is None:
            raise PreviewRefused(PreviewRefusal.AVATAR_NOT_READY)

        outfit = self._chosen_outfit(decision)
        candidate_key, candidate_slot = await self._candidate_garment(decision)
        return _RenderInputs(
            candidate_object_key=candidate_key,
            candidate_slot=candidate_slot,
            garments=outfit,
            avatar_version_id=avatar_version_id,
        )

    async def _active_avatar_version(self, user_id: uuid.UUID) -> uuid.UUID | None:
        """The version id a preview would render, or None if there is none.

        Both halves are required and they are not the same check. The status
        whitelist is contract §6.1's rule (`ready` / `refresh_recommended`);
        `active_version_id` is what the key actually needs. A `disabled` profile
        keeps its row as a deletion tombstone, and `refresh_recommended` is a
        usable-but-ageing likeness the contract explicitly admits, so neither
        can be inferred from the other.
        """
        row = (
            await self.session.execute(
                select(AvatarProfile.status, AvatarProfile.active_version_id).where(
                    AvatarProfile.user_id == user_id
                )
            )
        ).first()
        if row is None:
            return None
        status, version_id = row
        if status not in {s.value for s in AVATAR_STATUSES_WITH_ACTIVE_VERSION}:
            return None
        if version_id is None:
            return None
        # The version row must still exist: `active_version_id` is ON DELETE SET
        # NULL, but a race between a `replace` swap and this read could hand us
        # an id whose canonical render the worker cannot load.
        exists: uuid.UUID | None = await self.session.scalar(
            select(AvatarVersion.id).where(AvatarVersion.id == version_id)
        )
        return exists

    @staticmethod
    def _chosen_outfit(decision: WardrobeDecision) -> tuple[tuple[uuid.UUID, str], ...]:
        """The supporting items, read out of the immutable snapshot.

        **From the snapshot, never recomputed.** V3-003 makes a decision
        append-only precisely so its explanation cannot drift; a preview
        composed from today's wardrobe would show an outfit the decision never
        mentioned, and the cache key would change every time an unrelated item
        was added. `outfits[0]` is the one V3-006 ranked first and V3-007 already
        reasoned from — the same outfit the user was shown.

        `alternatives` are deliberately not eligible: they are anchored on an
        owned item *instead of* the candidate, so rendering one would put a
        preview of something the user already owns behind a "see this purchase
        on me" request.
        """
        evidence = decision.evidence_snapshot or {}
        outfit_evidence = evidence.get("outfit_evidence") or {}
        outfits = outfit_evidence.get("outfits") or []
        if not outfits:
            raise PreviewRefused(PreviewRefusal.NO_GROUNDED_OUTFIT)
        slots = outfits[0].get("slots") or []
        garments = tuple(
            (uuid.UUID(str(s["item_id"])), str(s["slot"])) for s in slots if s.get("item_id")
        )
        if not garments:
            raise PreviewRefused(PreviewRefusal.NO_GROUNDED_OUTFIT)
        return garments

    async def _candidate_garment(self, decision: WardrobeDecision) -> tuple[str, str]:
        """The candidate's durable image key and the slot it occupies.

        Refuses when the candidate is a link — see the module docstring. The
        slot comes from the parsed category via V3-006's own `CATEGORY_SLOTS`,
        taking the first entry because a dress occupies torso *and* legs while a
        `GarmentImage` carries one slot; torso is the slot `FILL_ORDER` fills
        first and the one a dress is rendered from.
        """
        if decision.candidate_id is None:
            raise PreviewRefused(PreviewRefusal.NO_RENDERABLE_CANDIDATE)
        row = (
            await self.session.execute(
                select(PurchaseCandidate.source_object_key, PurchaseCandidate.parsed_item).where(
                    PurchaseCandidate.id == decision.candidate_id
                )
            )
        ).first()
        if row is None or not row[0]:
            raise PreviewRefused(PreviewRefusal.NO_RENDERABLE_CANDIDATE)
        object_key, parsed = row
        category = str((parsed or {}).get("category") or "")
        slots = CATEGORY_SLOTS.get(category, ())
        if not slots:
            # An unparsed or unknown category has no slot, so the renderer would
            # not know where to put the garment. Refused rather than guessed:
            # `torso` would be a coin flip that puts trousers on a chest.
            raise PreviewRefused(PreviewRefusal.NO_RENDERABLE_CANDIDATE)
        slot = slots[0]
        # The port's vocabulary is the fence: V3-016 asserts GarmentSlot equals
        # V3-006's FILL_ORDER, so this can only fail if one of them moved.
        GarmentSlot(slot)
        return str(object_key), slot

    async def _cached_asset(self, key: StaticPreviewCacheKey) -> OutfitVisualAsset | None:
        return cast(
            OutfitVisualAsset | None,
            await self.session.scalar(
                select(OutfitVisualAsset).where(OutfitVisualAsset.cache_key == key.digest())
            ),
        )

    # --- Writes -----------------------------------------------------------

    async def _serve_cached(
        self,
        asset: OutfitVisualAsset,
        *,
        user_id: uuid.UUID,
        correlation_id: str | None,
    ) -> PreviewRequested:
        """Contract §6.1's cache hit — plus the one case that is not a hit.

        An idempotency key that never expires is right for work that *succeeded*
        and wrong for work that died: the same avatar and the same outfit
        compose the same key forever, so a dead job would own that key and every
        later request would answer 200 with a preview that will never arrive.
        V3-013 hit this exact wall and recorded the fix
        (`PrepareAvatar._revive_if_terminal`); the same reasoning applies here,
        so a *failed* preview is retryable and a *ready* one is free.

        Reviving is new paid work, so it goes through the quota. A cache hit
        does not, which is the ordering the module docstring calls load-bearing.
        """
        job = await self._backing_job(asset.id)
        revivable = job is not None and job.status in (JOB_STATUS_DEAD, JOB_STATUS_FAILED)
        if not revivable:
            logger.info(
                "static_tryon.cache_hit",
                asset_id=str(asset.id),
                generation_status=asset.generation_status,
            )
            return PreviewRequested(
                asset_id=asset.id,
                generation_status=asset.generation_status,
                created=False,
            )

        assert job is not None  # narrowed by `revivable`
        await assert_within_quota(self.session, user_id, JOB_TYPE)
        if not await self._revive(job):
            # Another retry won the conditional UPDATE. Its work is live, so
            # this request started nothing — the honest answer, and the same
            # `rowcount != 1` resolution V3-013 uses.
            return PreviewRequested(
                asset_id=asset.id,
                generation_status=asset.generation_status,
                created=False,
            )
        asset.generation_status = STATUS_QUEUED
        # The previous failure's message would otherwise sit on a row that is
        # now queued again, and `GET /v1/outfit-visual-assets/{id}` reads both.
        asset.error_code = None
        asset.error_message = None
        await self.session.flush()
        logger.info(
            "static_tryon.revived", asset_id=str(asset.id), correlation_id=correlation_id or ""
        )
        return PreviewRequested(
            asset_id=asset.id, generation_status=STATUS_QUEUED, created=True
        )

    async def _insert_or_replay(
        self,
        key: StaticPreviewCacheKey,
        inputs: _RenderInputs,
        identity: PipelineIdentity,
        *,
        user_id: uuid.UUID,
        decision_id: uuid.UUID,
        correlation_id: str | None,
    ) -> PreviewRequested:
        """Insert the asset + its job, or lose the race and read the winner.

        The two writes share this request's transaction, so a crash between them
        cannot leave an asset with no job behind it — which would spin on
        `queued` until the GET endpoint's 15-minute staleness rule called it
        failed.
        """
        # Built once and shared by the row and the job payload, so the worker
        # can never be handed a different set of inputs than the ones the row
        # records having been keyed on.
        input_snapshot: dict[str, Any] = {
            "decision_id": str(decision_id),
            "avatar_version_id": str(inputs.avatar_version_id),
            "view_type": VIEW_TYPE,
            # Stable storage keys and item ids only — never a signed URL and
            # never image bytes (contract §7). `input_snapshot` is a persisted
            # column that outlives log retention.
            "candidate_object_key": inputs.candidate_object_key,
            "candidate_slot": inputs.candidate_slot,
            "garments": [
                {"item_id": str(item_id), "slot": slot} for item_id, slot in inputs.garments
            ],
        }
        asset = OutfitVisualAsset(
            user_id=user_id,
            visual_type=VIEW_TYPE,
            generation_status=STATUS_QUEUED,
            cache_key=key.digest(),
            # Written at insert, not after the render, because migration 0048's
            # CHECK requires a `static_tryon` row to name its pipeline — and
            # because §6.3 already needs the triple here to compose the key.
            # V3-016 recorded this constraint as covering "a write path V3-017
            # owns"; this is that path.
            provider=identity.provider,
            model_version=identity.model_version,
            prompt_version=identity.prompt_version,
            input_snapshot=input_snapshot,
        )
        try:
            async with self.session.begin_nested():
                self.session.add(asset)
                await self.session.flush()
        except IntegrityError:
            # 23505 on `uq_outfit_visual_assets_cache_key`: a concurrent request
            # for the identical preview inserted first. A race, not a bug — the
            # SAVEPOINT contained it, so the session is still usable and we read
            # the winner back rather than paying for a second render.
            winner = await self._cached_asset(key)
            if winner is None:  # pragma: no cover - the index is what raised
                raise
            logger.info("static_tryon.lost_insert_race", asset_id=str(winner.id))
            return await self._serve_cached(
                winner, user_id=user_id, correlation_id=correlation_id
            )

        job, created = await enqueue(
            self.session,
            job_type=JOB_TYPE,
            idempotency_key=key.idempotency_key(),
            payload={
                "user_id": str(user_id),
                "asset_id": str(asset.id),
                **input_snapshot,
                # Readable here because the key carries only its digest — the
                # V3-013 precedent. Internal (contract §6.4): job payload, never
                # a response.
                "pipeline_identity": identity.as_slug(),
            },
            user_id=user_id,
            target_id=asset.id,
            correlation_id=correlation_id,
            max_attempts=MAX_ATTEMPTS,
        )
        logger.info(
            "static_tryon.enqueued",
            asset_id=str(asset.id),
            job_created=created,
            provider=identity.provider,
            model_version=identity.model_version,
        )
        return PreviewRequested(
            asset_id=asset.id, generation_status=STATUS_QUEUED, created=True
        )

    async def _backing_job(self, asset_id: uuid.UUID) -> AsyncJob | None:
        return cast(
            AsyncJob | None,
            await self.session.scalar(
                select(AsyncJob).where(
                    AsyncJob.job_type == JOB_TYPE, AsyncJob.target_id == asset_id
                )
            ),
        )

    async def _revive(self, job: AsyncJob) -> bool:
        """Re-open a preview job that died. V3-013's three guards, unchanged.

        Only from a terminal failure (`succeeded` is excluded, which is what
        keeps acceptance 1 true), only under a conditional UPDATE so two
        concurrent retries yield exactly one live job, and `attempts` resets
        while `last_error` is preserved as the record of why the user retried.
        """
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
        # Updated behind the ORM's back; without this the identity map keeps
        # serving the terminal status to anything reading `job` later.
        await self.session.refresh(job)
        return True
