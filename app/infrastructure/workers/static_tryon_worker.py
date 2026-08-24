"""V3-017 — run static try-on previews out of band, with bounded retries.

Consumes `async_jobs` rows of type `tryon` enqueued by
`app.application.purchase.request_static_preview`. The request that asked for a
preview calls no provider; everything expensive happens here on its own
`AsyncSessionLocal` session, the `avatar_preparation_worker` shape.

**The lifecycle this worker implements is contract §6.2's, complete since
V3-018:** `queued → processing → quality_check → ready | failed |
rejected_by_quality_gate`. After the render succeeds the row flips to
`quality_check`, the `VisualQualityGatePort` measures the image, and
`visual_quality_rules.evaluate_render` decides show or hide (V3_SPEC §14's
"hide unreliable generations rather than returning every successful provider
response"). On HIDE the worker re-renders at most
`TRYON_QUALITY_MAX_AUTO_REGENERATIONS` times (default 1 — contract §6.3's "at
most one model retry after a quality-gate failure") and then writes
`rejected_by_quality_gate`.

**Three properties of the rejected path worth stating plainly:**

1. **A rejected render is never uploaded.** The gate runs on the bytes in
   memory, before `put_object`, so a hidden image has no `object_key`, no
   stored PNG and nothing `GET /v1/outfit-visual-assets/{asset_id}` could ever
   sign — acceptance 2 ("low-quality output is not exposed through image_url")
   is structural, not a read-time filter. `error_code` stays NULL: §6.2's
   frozen shape says "null unless failed", and the degradation rule makes FE
   render the rejection from `generation_status` alone.
2. **A rejection is a *succeeded* job.** The worker did its work; the outcome
   is a verdict, not an error. Marking it failed would hand the technical
   retry budget (`MAX_ATTEMPTS`) to the quality loop and multiply paid calls —
   and because the job is `succeeded`, `RequestStaticPreview._serve_cached`
   will never revive it: the same key serves the same rejection from cache,
   which is §6.3's "identical inputs never regenerate" applied to a render we
   refused to show. A new prompt/model version composes a new key and gets a
   fresh chance; that is the designed escape hatch.
3. **A gate that *raises* is a technical failure, not a verdict.** The
   exception rides the existing `mark_failed` path: retries, then dead-letter,
   then `failed` on the row. Failing closed rather than open is the feature's
   point — a gate outage must not mean "ship every render unchecked" — and
   failing *technically* rather than writing `rejected_by_quality_gate` keeps
   the rejected state meaning "a gate looked at this and said no".

The persisted `quality_signals` (migration 0050) carry the verdict, the
per-check grades, neutral render measurements and the regeneration accounting —
every payload passes `validate_render_signals`, so no image data and no
judgement about the person can reach the column or the log line (acceptance 4;
contract §7).

**Why the terminal word is `ready` and not M10's `completed`.** Contract §6.2
names `ready` for V3 assets, and §1's additive-only rule forbids rewriting the
M10 rows that already say `completed`. So one column carries two terminal words,
one per generation of writer, and the GET endpoint passes both through
unchanged (its `generation_status` is a plain `str`, not an enum). Rewriting
M10's word to match would be exactly the "fix it piecemeal" the repo's API
conventions warn against.

**Dead-letter behaviour is observable without this worker doing anything
special** (V3-017 acceptance 2), through three mechanisms that already exist and
one that does not:

- `job_runtime.mark_failed` moves the row to `dead` once `attempts` reaches
  `max_attempts`, and `_alert_dead` raises the Sentry event oncall triages on.
- `recover_expired_leases` dead-letters a crash-looping job that never reached
  `mark_failed` at all, preserving `attempts`.
- `GET /v1/outfit-visual-assets/{asset_id}` already resolves *any* asset whose
  `target_id` job is `dead` to `failed` on read (`_resolve_pending_status`), so
  the user-visible state converges immediately rather than at the next cron.
- `reconcile_dead_static_tryon_assets` (below) is the durable half: it writes
  `failed` onto the row, so the state is true in the database and not only in
  one endpoint's read-time projection. Both exist for the same reason the
  `outfit_board` pair does.

**A dead preview is not a dead end.** `RequestStaticPreview._serve_cached`
revives a terminal job when the user asks again, so the permanent cache-key
tombstone V3-013 hit cannot happen here.

**What CI exercises.** `FakeStaticTryOn` is local, deterministic and the only
adapter in the tree (contract §6.4), so `queued → processing → ready` runs for
real in tests — no container, no network, no spend. What is *not* proven
anywhere: that a production model renders a believable try-on. That is the
budgeted, human-gated provider evaluation, the same honest limit V3-013 and
V3-016 recorded for their fakes.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy import exists, select, update

from app.application.paid_generation_admission import settle_terminal_reservations
from app.application.ports.static_tryon import (
    GarmentImage,
    GarmentSlot,
    PipelineIdentity,
    PoseTemplate,
    StaticTryOnPort,
    StaticTryOnRequest,
    ViewRequirement,
)
from app.application.ports.visual_quality_gate import VisualQualityGatePort
from app.application.purchase.visual_quality_rules import (
    RenderVerdict,
    evaluate_render,
    finalize_render_signals,
)
from app.bootstrap.container import object_storage, static_tryon, visual_quality_gate
from app.common.analytics_emit import emit_v3_event
from app.common.analytics_events import V3AnalyticsEventName, resolve_flow_id
from app.config.settings import get_settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.avatar import AvatarVersion
from app.infrastructure.persistence.models.outfit_visual import OutfitVisualAsset
from app.infrastructure.persistence.models.wardrobe import WardrobeItemImage
from app.infrastructure.persistence.repositories.outfit_visual_repo import (
    SqlAlchemyOutfitVisualRepository,
)
from app.infrastructure.persistence.session import AsyncSessionLocal
from app.infrastructure.storage.object_key import static_preview_object_key
from app.infrastructure.workers.job_runtime import (
    JOB_STATUS_DEAD,
    JOB_STATUS_RUNNING,
    claim_one,
    mark_failed,
    mark_succeeded,
)

logger = structlog.get_logger(__name__)

JOB_TYPE = "tryon"
MAX_DRAIN_PER_TICK = 5

STATUS_PROCESSING = "processing"
STATUS_QUALITY_CHECK = "quality_check"
STATUS_READY = "ready"
STATUS_FAILED = "failed"
STATUS_REJECTED = "rejected_by_quality_gate"

#: Every non-terminal `generation_status` a V3 try-on row can sit in. The
#: dead-letter reconciler and the GET endpoint's read-time projection both key
#: on this set: a worker crash mid-gate leaves the row on `quality_check`, and
#: a poller must converge to `failed` from there exactly as it does from
#: `processing` (§6.2 says polling stops only on the three terminal states).
PENDING_STATUSES = ("queued", STATUS_PROCESSING, STATUS_QUALITY_CHECK)


def _emit_outcome(
    user_id: uuid.UUID,
    correlation_id: str | None,
    generation_status: str,
    retry_count: int,
) -> None:
    """V3-019 — `v3_preview_outcome`, once per generation, at the terminal state.

    The analytics contract narrows this event to terminal states only (counting
    intermediate polls would inflate every preview denominator), which is why it
    is emitted here — where the terminal write happens — and never from the GET
    endpoint. `flow_id` is required on the event: a render whose enqueue carried
    no correlation id has nothing to join, so it is silently not emitted rather
    than validated-and-dropped with a warning per render. `retry_count` is the
    quality loop's regeneration count, clamped to the contract's 0..1 bound so
    an explicitly raised `TRYON_QUALITY_MAX_AUTO_REGENERATIONS` cannot produce
    an event the frozen validator rejects. Grades and counts only — no keys, no
    URLs, no image bytes (contract §7).
    """
    flow_id = resolve_flow_id(correlation_id)
    if flow_id is None:
        return
    emit_v3_event(
        V3AnalyticsEventName.PREVIEW_OUTCOME.value,
        {
            "generation_status": generation_status,
            "cache_hit": False,
            "retry_count": min(max(retry_count, 0), 1),
        },
        user_id=user_id,
        flow_id=flow_id,
    )


class _PermanentPayloadError(Exception):
    """A payload no retry can fix. Fails the job without quoting the value."""


async def _process_one(
    job: Any,
    *,
    port: StaticTryOnPort | None = None,
    gate: VisualQualityGatePort | None = None,
) -> None:
    payload: dict[str, Any] = job.payload or {}
    structlog.contextvars.bind_contextvars(
        correlation_id=job.correlation_id or "",
        job_id=str(job.id),
        asset_id=str(payload.get("asset_id", "")),
    )
    try:
        try:
            user_id = uuid.UUID(payload["user_id"])
            asset_id = uuid.UUID(payload["asset_id"])
            avatar_version_id = uuid.UUID(payload["avatar_version_id"])
            candidate_object_key = str(payload["candidate_object_key"])
            candidate_slot = GarmentSlot(str(payload["candidate_slot"]))
            garments = [
                (uuid.UUID(str(g["item_id"])), GarmentSlot(str(g["slot"])))
                for g in payload.get("garments", [])
            ]
            if not candidate_object_key:
                raise _PermanentPayloadError
        except (KeyError, ValueError, TypeError, _PermanentPayloadError):
            # A fixed string, never the exception text: `uuid.UUID("…")` quotes
            # the offending value back, and `async_jobs.last_error` is a
            # persisted column that outlives log retention — the V3-012 lesson,
            # and this payload carries storage keys.
            async with AsyncSessionLocal() as session:
                await mark_failed(
                    session, job.id, claimed_attempt=job.attempts, error="invalid_payload"
                )
                await session.commit()
            return

        try:
            # 1. Flip the read model to processing, and re-check the row is
            #    still there. A user who deleted the decision (or the account)
            #    between enqueue and claim must not have a render generated.
            async with AsyncSessionLocal() as session:
                repo = SqlAlchemyOutfitVisualRepository(session)
                asset = await repo.get_by_id(asset_id)
                if asset is None or asset.user_id != user_id:
                    await mark_failed(
                        session, job.id, claimed_attempt=job.attempts, error="asset_not_found"
                    )
                    await session.commit()
                    logger.warning("static_tryon.asset_not_found")
                    return
                await repo.update_status(asset_id, STATUS_PROCESSING)
                await session.commit()

            # 2. Resolve every input to bytes, then render — no DB session held
            #    across the provider call, which can run long.
            avatar_key, garment_keys = await _resolve_object_keys(
                avatar_version_id=avatar_version_id,
                garments=garments,
            )
            storage = object_storage()
            renderer = port if port is not None else static_tryon()
            supporting: list[GarmentImage] = []
            for key, slot in garment_keys:
                supporting.append(GarmentImage(image=await storage.get_object(key), slot=slot))
            request = StaticTryOnRequest(
                avatar_image=await storage.get_object(avatar_key),
                candidate_garment=GarmentImage(
                    image=await storage.get_object(candidate_object_key), slot=candidate_slot
                ),
                supporting_garments=tuple(supporting),
                pose_template=PoseTemplate.NEUTRAL_FULL_BODY,
                view=ViewRequirement.FRONT,
            )
            result = await renderer.render(request)

            # 3. What ran must be what the key promised. `result.identity`
            #    comes from the adapter that actually rendered; the row's triple
            #    was written at enqueue time from the adapter the container
            #    selected then, and both are components of the cache key. If
            #    they disagree, the container was re-pointed mid-flight and this
            #    image does NOT belong under this key — serving it would let a
            #    cache hit return a picture from a pipeline the key does not
            #    describe, permanently, since the row is never re-rendered.
            #    Refused rather than overwritten: overwriting would make the row
            #    self-consistent and the *cache* wrong, which is the harder
            #    failure to ever notice. Failing it dead-letters this job, and
            #    the user's next request composes a new key from the new
            #    identity and gets a correct render under it.
            await _assert_identity_matches(asset_id, result.identity)

            # 4. The quality gate (V3-018), on the bytes in memory — BEFORE any
            #    upload, so a hidden render never has an object to expose. The
            #    row flips to `quality_check` first (§6.2's frozen intermediate
            #    state; with a real vision-model gate this is the state a
            #    poller sees while it runs), and stays there through a
            #    regeneration — the frozen diagram has no backward transition
            #    to `processing`, and inventing one would tell a poller the
            #    gate had not happened yet.
            #
            #    FENCED on this attempt still owning the job, atomically in the
            #    UPDATE's WHERE, unlike the step-1 `processing` write (which
            #    happens on a claim milliseconds old). The render is the long
            #    call, so the lease is most likely to have been recovered right
            #    here — and an unfenced intermediate write from a disowned
            #    attempt could land AFTER the new attempt's terminal write,
            #    stranding a `succeeded` job's row on `quality_check` forever:
            #    non-terminal, so pollers never stop, and invisible to the
            #    dead-job reconciler, which only converges rows whose job is
            #    dead. The terminal writes were always fenced (mark_succeeded /
            #    mark_failed); this closes the same door for the intermediate
            #    one, and a disowned attempt also stops paying for gate calls
            #    and an upload whose finalize was already doomed.
            async with AsyncSessionLocal() as session:
                flipped = await session.scalar(
                    update(OutfitVisualAsset)
                    .where(
                        OutfitVisualAsset.id == asset_id,
                        exists().where(
                            AsyncJob.id == job.id,
                            AsyncJob.status == JOB_STATUS_RUNNING,
                            AsyncJob.attempts == job.attempts,
                        ),
                    )
                    .values(generation_status=STATUS_QUALITY_CHECK)
                    .returning(OutfitVisualAsset.id)
                )
                await session.commit()
            if flipped is None:
                # Two ways to get here, told apart by re-reading the asset:
                # the row was purged mid-render (same answer the ready path
                # gives, minus the object cleanup — nothing was uploaded yet),
                # or the lease was recovered and another attempt owns the job
                # now, in which case this attempt must simply stop.
                async with AsyncSessionLocal() as session:
                    repo = SqlAlchemyOutfitVisualRepository(session)
                    if await repo.get_by_id(asset_id) is None:
                        await mark_failed(
                            session,
                            job.id,
                            claimed_attempt=job.attempts,
                            error="asset_not_found",
                        )
                        await session.commit()
                        logger.warning("static_tryon.asset_deleted_discarding_render")
                    else:
                        logger.warning("static_tryon.lease_lost_skipping_quality_gate")
                return

            gate_port = gate if gate is not None else visual_quality_gate()
            decision = evaluate_render(await gate_port.observe(result.image, request))
            render_count = 1
            # Acceptance 3: at most one automatic regeneration unless
            # explicitly configured. Each pass re-renders and re-judges; with
            # the deterministic P0 fake the second render is byte-identical
            # and the loop exists for the stochastic provider the human gate
            # may later select. The identity check is not re-run: `renderer`
            # is one object for the whole execution, so a regeneration cannot
            # come from a different pipeline than the first render.
            regen_budget = get_settings().TRYON_QUALITY_MAX_AUTO_REGENERATIONS
            while decision.verdict is RenderVerdict.HIDE and render_count <= regen_budget:
                logger.info(
                    "static_tryon.quality_regeneration",
                    failed_checks=decision.signals.get("failed_checks", []),
                    render_count=render_count,
                )
                result = await renderer.render(request)
                decision = evaluate_render(await gate_port.observe(result.image, request))
                render_count += 1
            quality_signals = finalize_render_signals(
                decision.signals, render_count=render_count, regen_budget=regen_budget
            )

            if decision.verdict is RenderVerdict.HIDE:
                await _finalize_rejected(job, asset_id, quality_signals, user_id=user_id)
                return

            # 5. Storage first, row second — the `avatar_preparation_worker`
            #    ordering, for the same reason: the reverse can produce a
            #    `ready` asset pointing at an object that does not exist, while
            #    this order's worst case is an orphaned object that the account
            #    prefix sweep reaches.
            object_key = static_preview_object_key(user_id, asset_id)
            await storage.put_object(object_key, result.image, "image/png")

            # 6. Persist + finalize, fenced on this attempt's claim. If the
            #    lease was recovered while we were rendering, `mark_succeeded`
            #    returns False and we MUST NOT commit `ready`: another attempt
            #    owns the asset now, or oncall already sees the job as dead.
            async with AsyncSessionLocal() as session:
                repo = SqlAlchemyOutfitVisualRepository(session)
                # The row is re-read, not just written through. `update_status`
                # is a silent no-op on a missing row, so without this a render
                # that finished *after* `DELETE /v1/avatar-profile?purge_derived=
                # true` swept the row would leave the PNG in the bucket with
                # nothing pointing at it — a likeness the user revoked, kept.
                # The purge also enqueues this exact key (it is derived from the
                # asset id, so the sweep can name it before the render exists),
                # so this closes the window from the other side and the two do
                # not depend on each other's timing.
                if await repo.get_by_id(asset_id) is None:
                    # Idempotent by contract, so racing the purge's own delete
                    # is a no-op rather than an error.
                    await storage.delete_objects([object_key])
                    await mark_failed(
                        session, job.id, claimed_attempt=job.attempts, error="asset_not_found"
                    )
                    await session.commit()
                    logger.warning("static_tryon.asset_deleted_discarding_render")
                    return
                await repo.update_status(
                    asset_id,
                    STATUS_READY,
                    object_key=object_key,
                    quality_signals=quality_signals,
                )
                ok = await mark_succeeded(session, job.id, claimed_attempt=job.attempts)
                if not ok:
                    await session.rollback()
                    # Deliberately NOT deleting the object here, unlike the
                    # branch above. The key is derived from the asset id, so the
                    # attempt that now owns this asset writes the *same* key —
                    # deleting it would race that attempt into a `ready` row
                    # pointing at nothing. An orphan is recoverable; a broken
                    # `ready` row is not, and no row references this object
                    # unless the new attempt succeeds and re-creates it.
                    logger.warning("static_tryon.lease_lost_discarding_completion")
                    return
                await session.commit()

            # Provenance and status only — no keys, no URLs, no image bytes and
            # nothing about the person (contract §7). `provider` /
            # `model_version` are internal columns and this is an internal log,
            # not a response (contract §6.4) — the same boundary
            # `avatar_preparation_worker` draws. `render_count` says what the
            # quality loop cost; the signals themselves are on the row.
            logger.info(
                "static_tryon.completed",
                provider=result.identity.provider,
                model_version=result.identity.model_version,
                supporting_garment_count=len(garment_keys),
                render_count=render_count,
            )
            _emit_outcome(user_id, job.correlation_id, STATUS_READY, render_count - 1)

        except Exception as exc:
            logger.exception("static_tryon.failed", error_type=type(exc).__name__)
            async with AsyncSessionLocal() as session:
                outcome = await mark_failed(
                    session, job.id, claimed_attempt=job.attempts, error=type(exc).__name__
                )
                await session.commit()
            # Only a TERMINAL failure moves the read model. While attempts
            # remain the job is coming back after backoff, and showing the user
            # `failed` with real work still queued behind it would be a lie in
            # the discouraging direction. The asset stays `processing` until
            # then — which is also what lets a retry flip it back.
            if outcome == JOB_STATUS_DEAD:
                async with AsyncSessionLocal() as session:
                    repo = SqlAlchemyOutfitVisualRepository(session)
                    await repo.update_status(
                        asset_id,
                        STATUS_FAILED,
                        error_code="generation_failed",
                        error=type(exc).__name__,
                    )
                    await session.commit()
                _emit_outcome(user_id, job.correlation_id, STATUS_FAILED, 0)
    finally:
        structlog.contextvars.unbind_contextvars("correlation_id", "job_id", "asset_id")


async def _finalize_rejected(
    job: Any, asset_id: uuid.UUID, quality_signals: dict[str, Any], *, user_id: uuid.UUID
) -> None:
    """Write `rejected_by_quality_gate` — a verdict, so the job *succeeds*.

    Mirrors the ready path's finalize discipline with nothing to upload:

    - The row is re-read first. If `DELETE /v1/avatar-profile?purge_derived=
      true` swept it while the gate was judging, there is nothing to write and
      nothing was ever uploaded — `mark_failed(asset_not_found)`, the same
      answer the ready path gives, and no storage cleanup because the rejected
      path never created an object.
    - `mark_succeeded` is fenced on this attempt's claim. If the lease was
      recovered mid-gate, another attempt owns the asset now and this verdict
      must not overwrite whatever that attempt concludes.
    - `error_code` / `error_message` stay NULL — contract §6.2's frozen shape
      says "null unless failed", and FE renders the rejection from
      `generation_status` alone (the frozen degradation rule).
    """
    async with AsyncSessionLocal() as session:
        repo = SqlAlchemyOutfitVisualRepository(session)
        if await repo.get_by_id(asset_id) is None:
            await mark_failed(
                session, job.id, claimed_attempt=job.attempts, error="asset_not_found"
            )
            await session.commit()
            logger.warning("static_tryon.asset_deleted_discarding_verdict")
            return
        await repo.update_status(asset_id, STATUS_REJECTED, quality_signals=quality_signals)
        ok = await mark_succeeded(session, job.id, claimed_attempt=job.attempts)
        if not ok:
            await session.rollback()
            logger.warning("static_tryon.lease_lost_discarding_verdict")
            return
        await session.commit()
    # Grades and counts only — the same contract §7 boundary as the completed
    # log. What failed is actionable for an operator; the image is not here.
    logger.info(
        "static_tryon.rejected_by_quality_gate",
        failed_checks=quality_signals.get("failed_checks", []),
        undetermined_checks=quality_signals.get("undetermined_checks", []),
        render_count=quality_signals.get("render_count"),
    )
    _emit_outcome(
        user_id,
        job.correlation_id,
        STATUS_REJECTED,
        int(quality_signals.get("render_count") or 1) - 1,
    )


async def _assert_identity_matches(asset_id: uuid.UUID, identity: PipelineIdentity) -> None:
    """Raise when the rendering pipeline is not the one the row's key names.

    Unreachable in P0 — `container.static_tryon()` has exactly one adapter and
    no provider switch, because selecting a real provider is V3-016's human
    gate. It is here rather than deferred because the day that gate opens is
    the day a deploy can re-point the container while jobs are queued, and the
    alternative to this check is not "nothing happens", it is a wrong image
    cached forever under a key that describes a different model.
    """
    async with AsyncSessionLocal() as session:
        row = (
            await session.execute(
                select(
                    OutfitVisualAsset.provider,
                    OutfitVisualAsset.model_version,
                    OutfitVisualAsset.prompt_version,
                ).where(OutfitVisualAsset.id == asset_id)
            )
        ).first()
    if row is None:
        raise LookupError("asset_disappeared_mid_render")
    if tuple(row) != (identity.provider, identity.model_version, identity.prompt_version):
        # The values themselves are internal but not sensitive (contract §6.4
        # keeps them out of responses, not out of logs — the
        # `avatar_preparation_worker` precedent), and an operator cannot act on
        # "a mismatch happened" without them.
        logger.error(
            "static_tryon.pipeline_identity_mismatch",
            enqueued_provider=row[0],
            enqueued_model_version=row[1],
            rendered_provider=identity.provider,
            rendered_model_version=identity.model_version,
        )
        raise RuntimeError("pipeline_identity_mismatch")


async def _resolve_object_keys(
    *,
    avatar_version_id: uuid.UUID,
    garments: list[tuple[uuid.UUID, GarmentSlot]],
) -> tuple[str, list[tuple[str, GarmentSlot]]]:
    """Stable object keys for the avatar render and each supporting garment.

    Resolved here rather than frozen into the payload because an item's image
    can be re-cut (the cutout pipeline rewrites `object_key`), and a payload
    written days ago would then point at an object that no longer exists. The
    *cache key* is unaffected: it names item ids, not their current images.

    One `IN` query for all garments, not one per item — the N+1 that
    `load_wear_scenes` exists to prevent, in a loop that runs per render.

    A garment whose image is missing is skipped rather than fatal: a wardrobe
    item with no stored image is a real state (manually added, never
    photographed), and refusing the whole preview because one supporting item
    has no picture would deny the user the preview over a detail the renderer
    can simply omit. The candidate is not optional — it is what the preview is
    *about* — and its absence was already refused at request time.
    """
    async with AsyncSessionLocal() as session:
        version = await session.get(AvatarVersion, avatar_version_id)
        if version is None or not version.canonical_front_object_key:
            # The avatar was deleted or replaced between enqueue and claim.
            # Retrying cannot fix it, but `mark_failed` will exhaust the
            # attempts and dead-letter — acceptable, and far better than
            # rendering against a likeness the user removed.
            raise LookupError("avatar_version_unavailable")
        avatar_key = str(version.canonical_front_object_key)

        if not garments:
            return avatar_key, []
        rows = (
            await session.execute(
                select(WardrobeItemImage.item_id, WardrobeItemImage.object_key)
                .where(
                    WardrobeItemImage.item_id.in_([item_id for item_id, _ in garments]),
                    WardrobeItemImage.object_key.is_not(None),
                )
                .order_by(WardrobeItemImage.item_id, WardrobeItemImage.created_at.desc())
            )
        ).all()

    # Most recent image per item. Deterministic by the ORDER BY above, so two
    # renders of the same outfit use the same pictures — which is what makes a
    # cache hit and a fresh render the same image rather than merely the same
    # key.
    by_item: dict[uuid.UUID, str] = {}
    for item_id, object_key in rows:
        by_item.setdefault(item_id, str(object_key))

    resolved = [
        (by_item[item_id], slot) for item_id, slot in garments if item_id in by_item
    ]
    if len(resolved) != len(garments):
        logger.info(
            "static_tryon.garment_image_missing",
            requested=len(garments),
            resolved=len(resolved),
        )
    return avatar_key, resolved


async def reconcile_dead_static_tryon_assets(ctx: object = None) -> int:
    """Write `failed` onto assets whose backing job dead-lettered.

    The GET endpoint already projects this at read time, so why persist it: a
    read-time projection is invisible to everything that is not that endpoint —
    analytics, support tooling, and any later reader of the row. This makes the
    state true in the database within one cron interval, the same division of
    labour `reconcile_dead_visual_assets` has for outfit boards.

    Not reused from `_visual_asset_runtime.reconcile_dead_visual_assets`
    verbatim because that helper hardcodes the M10 pending vocabulary and this
    row's error columns are populated differently; the shapes are close enough
    to read together and different enough that folding them would need a
    parameter per difference.
    """
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(
                    AsyncJob.target_id,
                    AsyncJob.last_error,
                    AsyncJob.correlation_id,
                    OutfitVisualAsset.user_id,
                )
                .join(OutfitVisualAsset, OutfitVisualAsset.id == AsyncJob.target_id)
                .where(
                    AsyncJob.job_type == JOB_TYPE,
                    AsyncJob.status == JOB_STATUS_DEAD,
                    AsyncJob.target_id.is_not(None),
                    # `quality_check` included since V3-018: a worker that
                    # crash-looped inside the gate leaves the row there, and it
                    # must converge to `failed` exactly as `processing` does.
                    OutfitVisualAsset.generation_status.in_(PENDING_STATUSES),
                )
            )
        ).all()
        if not rows:
            return 0
        repo = SqlAlchemyOutfitVisualRepository(session)
        for target_id, last_error, _correlation_id, _asset_user_id in rows:
            await repo.update_status(
                target_id,
                STATUS_FAILED,
                error_code="generation_failed",
                error=last_error or "job_dead_letter",
            )
        await session.commit()
    # V3-019 — the crash-loop generation's terminal moment IS this write:
    # `_process_one`'s failed-path emit never ran (the worker died mid-attempt
    # and lease recovery dead-lettered the job), and pending rows are the only
    # ones swept here, so this cannot double-emit with a path that already did.
    for _target_id, _last_error, correlation_id, asset_user_id in rows:
        _emit_outcome(asset_user_id, correlation_id, STATUS_FAILED, 0)
    logger.warning("static_tryon.reconciled_dead_assets", count=len(rows))
    return len(rows)


async def reconcile_static_tryon_reservations(ctx: object = None) -> int:
    """V3-026 acceptance 7 — settle reservations whose work has finished.

    A thin session wrapper; the policy is `paid_generation_admission
    .settle_terminal_reservations`, which derives each outcome from the job's
    status joined to the asset's `generation_status` rather than from anything a
    worker remembered to write. That is why a crashed or dead-lettered
    generation cannot leave budget permanently held: the sweep needs nothing
    from the process that died.

    Registered as its own cron AND called at the tail of the drain below, so
    accounting converges within the tick that produced the terminal state
    instead of at the next cron boundary. Both entry points are idempotent —
    the sweep only ever matches `outcome IS NULL`.
    """
    async with AsyncSessionLocal() as session:
        settled = await settle_terminal_reservations(session, job_type=JOB_TYPE)
        await session.commit()
    return settled


async def drain_static_tryon_jobs(ctx: object = None) -> int:
    processed = 0
    for _ in range(MAX_DRAIN_PER_TICK):
        async with AsyncSessionLocal() as session:
            job = await claim_one(session, [JOB_TYPE])
        if job is None:
            break
        await _process_one(job)
        processed += 1
    if processed:
        logger.info("static_tryon.drain_completed", processed=processed)
        # Every terminal state this tick produced is settled here, so the next
        # request sees the budget a failed render freed (V3-017 acceptance 3)
        # rather than waiting on the reconcile cron.
        await reconcile_static_tryon_reservations()
    return processed
