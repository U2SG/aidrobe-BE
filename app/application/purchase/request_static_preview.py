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

**QUOTA: an atomic reservation, since V3-026, and this diverges from a sibling.**

`ootd_posters.py:82` passes `redis_client` to `assert_within_quota`; this module
calls neither. Both of the alternatives were rejected for reasons that still
hold:

- The **Redis `INCRBY`** path is an *irrevocable* reservation, rolled back only
  when the guard itself refuses. Nothing refunds it when the work later fails,
  which would make a dead-lettered render permanently consume a unit of a
  5-per-day budget — contradicting V3-017 acceptance 3 and `cost_guard`'s own
  documented rule for call-count quotas. It is also not durable: the counter
  drains on a TTL, so nothing can be reconciled after a crash.
- The **DB aggregate** (`COUNT` over `async_jobs` in `succeeded`/`running`) has
  no side effect, but it cannot count a `queued` generation at all — and
  `queued` is a *persistent* state, not a momentary one. A job sits there until
  a drain tick claims it (`MAX_DRAIN_PER_TICK=5`, cron-paced), so sequential
  requests issued faster than the drain each counted zero and were each
  admitted, with no concurrency required. The bound was "the drain keeps up",
  which is not something the request path controls.

V3-026 replaces both with `paid_generation_admission`: one ledger row per unit
of purchase, taken inside a `pg_advisory_xact_lock` on
`(user_id, job_type, day)`, held from the moment of admission, settled from the
terminal truth that already exists on the job and the asset. That module's
docstring owns the design; what matters here is the *order*, below, and the two
things this module is responsible for getting right.

1. **A cache hit never reaches the admission module.** It is checked first and
   returns before any reservation exists (see "Quota is checked AFTER the cache
   read", below).
2. **A revive is a fresh purchase and a bounded one.** It takes its own
   reservation (`revive_index >= 1`) *and* is refused by
   `assert_revive_budget` once `TRYON_MAX_REVIVES` presses have been admitted
   for the same generation. Before V3-026 a dead job counted zero and stayed
   revivable, so pressing preview against a persistently failing provider
   bought another `MAX_ATTEMPTS` of provider calls each time, without bound.
   Charging the press meters the press; the cap is what bounds the loop.

**One limit stated plainly, because it is a policy decision and not an
oversight.** A `technical_failure` settles *without* consuming budget, because
V3-017 acceptance 3 ("failed calls do not burn call quota") is a passed
criterion and its reason is real — a provider outage must not lock a user out of
the retry they are about to press. So while a provider fails every render, a
user with N distinct decisions can still spend N × `MAX_ATTEMPTS` attempts in a
day. Closing that means deciding that failures *do* burn budget, which reverses
a passed criterion and is the `spending_budget` human gate's call. What V3-026
bounds is the same-generation loop and the instantaneous budget; this is
recorded so the residual is not rediscovered as a surprise.

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

from app.application.cost_guard import resolve_quota_limit
from app.application.paid_generation_admission import (
    Admission,
    assert_revive_budget,
    attach,
    release,
    reserve,
    settle_terminal_reservations,
)
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


def _quality_retry_count(asset: OutfitVisualAsset) -> int:
    """Regenerations the asset's render cost, from V3-018's accounting.

    ``render_count`` counts renders (1 = no regeneration); rows that predate
    the gate, or never rendered, carry no signals and cost no retries.
    """
    signals = asset.quality_signals or {}
    render_count = signals.get("render_count")
    if not isinstance(render_count, int) or render_count < 1:
        return 0
    return render_count - 1


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
    #: How many quality-gate regenerations the served asset's render cost
    #: (V3-018's `quality_signals.render_count` minus the first render), or 0
    #: when no render has happened or the signals predate the gate. Carried for
    #: V3-019's `v3_preview_outcome` cache-hit emission, which must report the
    #: original generation's retry accounting without a second query.
    quality_retry_count: int = 0


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
        consent_confirmed: bool = False,
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

        # Only now, with a cache miss confirmed, does this cost anything. The
        # budget is taken BEFORE the asset and the job exist, so no paid work is
        # ever created that a reservation did not admit (V3-026 acceptance 1);
        # `reserve` raises FriendlyException(429) instead of returning.
        admission = await reserve(
            self.session,
            user_id=user_id,
            job_type=JOB_TYPE,
            reservation_key=key.idempotency_key(),
            attempt_grant=MAX_ATTEMPTS,
            limit=resolve_quota_limit(JOB_TYPE),
        )
        return await self._insert_or_replay(
            key,
            inputs,
            identity,
            admission=admission,
            user_id=user_id,
            decision_id=decision_id,
            correlation_id=correlation_id,
            consent_confirmed=consent_confirmed,
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

        Reviving is new paid work, so it takes its own reservation AND is capped
        (V3-026 acceptance 5). A cache hit does neither, which is the ordering
        the module docstring calls load-bearing.
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
                quality_retry_count=_quality_retry_count(asset),
            )

        assert job is not None  # narrowed by `revivable`
        # Close the books on the attempt-set that just died, BEFORE re-opening
        # the job. Both halves of why are in `settle_terminal_reservations`'s
        # docstring: after `_revive` the job is `queued` again, so a still-held
        # reservation would eventually be settled from the NEXT attempt's status
        # and a failure would be recorded as a success — and the refund is what
        # makes the retry admissible at all when the day's budget is one.
        await settle_terminal_reservations(
            self.session, job_type=JOB_TYPE, target_id=asset.id
        )
        # The cap first, then the budget. Both raise 429 and they mean different
        # things: the cap says "this render keeps failing, stop pressing" and is
        # unconditional, the budget says "come back tomorrow" and only exists
        # when one is configured. Checking the cap first means a generation that
        # has exhausted its retries answers the same way whether or not the
        # user also happens to be out of daily budget.
        revive_index = await assert_revive_budget(
            self.session,
            target_id=asset.id,
            job_type=JOB_TYPE,
            max_revives=self.settings.TRYON_MAX_REVIVES,
        )
        admission = await reserve(
            self.session,
            user_id=user_id,
            job_type=JOB_TYPE,
            reservation_key=f"{job.idempotency_key}#revive:{revive_index}",
            attempt_grant=MAX_ATTEMPTS,
            limit=resolve_quota_limit(JOB_TYPE),
            revive_index=revive_index,
            target_id=asset.id,
        )
        if not await self._revive(job):
            # Another retry won the conditional UPDATE. Its work is live, so
            # this request started nothing — the honest answer, and the same
            # `rowcount != 1` resolution V3-013 uses. The unit this request
            # reserved bought nothing, so it is refunded rather than left held.
            if admission.created:
                await release(self.session, admission.reservation_id, reason="revive_lost_race")
            return PreviewRequested(
                asset_id=asset.id,
                generation_status=asset.generation_status,
                created=False,
            )
        await attach(self.session, admission.reservation_id, job_id=job.id, target_id=asset.id)
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
        admission: Admission,
        user_id: uuid.UUID,
        decision_id: uuid.UUID,
        correlation_id: str | None,
        consent_confirmed: bool = False,
    ) -> PreviewRequested:
        """Insert the asset + its job, or lose the race and read the winner.

        The three writes — reservation, asset, job — share this request's
        transaction, so a crash between any two cannot leave an asset with no
        job behind it (which would spin on `queued` until the GET endpoint's
        15-minute staleness rule called it failed), and cannot leave a
        reservation holding budget for work that does not exist.
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
            # V3-019 — the per-request consent from contract §6.1's frozen body,
            # persisted so the attestation that THIS render of the user's
            # likeness was asked for outlives the request. Distinct from
            # V3-013's avatar-level consent, which authorizes keeping the
            # reference, not any particular use of it (the V3-017 handoff).
            # False only on writers that predate the route.
            "consent_confirmed": consent_confirmed,
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
            # The winner's own reservation is what admitted this render, so the
            # unit reserved here bought nothing and is refunded. Only when this
            # call created it: an idempotent replay would be refunding the
            # request that won. Released BEFORE `_serve_cached`, because that
            # path may itself revive and must see the refunded budget.
            if admission.created:
                await release(
                    self.session, admission.reservation_id, reason="enqueue_lost_race"
                )
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
        # The reservation now names the work it paid for, which is what
        # `settle_terminal_reservations` follows to find the terminal truth.
        await attach(self.session, admission.reservation_id, job_id=job.id, target_id=asset.id)
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
