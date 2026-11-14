"""Shared `job_runtime` orchestration for `OutfitVisualAsset`-backed workers.

`outfit_board_worker` and `ootd_poster_worker` are twin verticals on
`job_runtime`: both back the same `OutfitVisualAsset` read model, run the same
state flow (queued → processing → completed/failed + dead reconcile), and differ
only in `JOB_TYPE` and the render step. This module owns the identical plumbing
(snapshot + flip to processing, render via injected `render_fn` with no DB
session held, lease-fenced finalize, dead-letter reconcile, drain loop); each
worker keeps just its constants, enqueue wrapper, and `render_fn`.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import structlog

from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.session import AsyncSessionLocal
from app.infrastructure.workers.job_runtime import (
    claim_one,
    mark_failed,
    mark_succeeded,
)

logger = structlog.get_logger(__name__)

RenderFn = Callable[["VisualAssetSnapshot"], Awaitable[tuple[str, str] | None]]


@dataclass
class VisualAssetSnapshot:
    id: uuid.UUID
    user_id: uuid.UUID
    input_snapshot: dict[str, Any]
    attempt: int = 1


async def process_visual_asset_job(
    job: AsyncJob,
    *,
    job_type: str,
    render_fn: RenderFn,
    log_prefix: str,
    asset_not_found_event: str,
) -> None:
    """Run a single claimed visual-asset job end-to-end. Each session is
    short-lived so a crash between calls doesn't poison subsequent claims."""
    from app.infrastructure.persistence.repositories.outfit_visual_repo import (
        SqlAlchemyOutfitVisualRepository,
    )

    payload: dict[str, Any] = job.payload or {}
    asset_id_str = payload.get("asset_id")
    if not asset_id_str:
        async with AsyncSessionLocal() as session:
            await mark_failed(session, job.id, claimed_attempt=job.attempts, error="missing asset_id in payload")
            await session.commit()
        return

    asset_id = uuid.UUID(asset_id_str)
    structlog.contextvars.bind_contextvars(
        correlation_id=job.correlation_id or "",
        job_id=str(job.id),
        asset_id=str(asset_id),
    )
    try:
        # 1. Snapshot input + flip read-model status to processing.
        async with AsyncSessionLocal() as session:
            repo = SqlAlchemyOutfitVisualRepository(session)
            asset = await repo.get_by_id(asset_id)
            if asset is None:
                logger.warning(asset_not_found_event, asset_id=str(asset_id))
                async with AsyncSessionLocal() as fail_session:
                    await mark_failed(
                        fail_session,
                        job.id,
                        claimed_attempt=job.attempts,
                        error="asset_not_found",
                    )
                    await fail_session.commit()
                return
            snapshot = VisualAssetSnapshot(
                id=asset.id,
                user_id=asset.user_id,
                input_snapshot=dict(asset.input_snapshot or {}),
                attempt=job.attempts,
            )
            await repo.update_status(asset_id, "processing")
            await session.commit()

        # 2. Render + upload (no DB session held during slow IO / external gen).
        try:
            rendered = await render_fn(snapshot)
        except Exception as exc:
            logger.warning(f"{log_prefix}_render_failed", error=str(exc))
            # Asset stays in 'processing' so a retry can flip it back; only on
            # dead does the read model land on 'failed' (via the runtime's
            # status return below).
            async with AsyncSessionLocal() as fail_session:
                new_status = await mark_failed(fail_session, job.id, claimed_attempt=job.attempts, error=str(exc))
                # If the runtime tells us the job is dead, surface that to the
                # read model so the GET endpoint shows 'failed' rather than an
                # indefinite 'processing'.
                if new_status == "dead":
                    repo = SqlAlchemyOutfitVisualRepository(fail_session)
                    await repo.update_status(asset_id, "failed", error=str(exc))
                await fail_session.commit()
            return

        # 3. Persist result + finalize. Fenced on this attempt's claim: if the
        # lease was recovered while we were rendering (renders can run long),
        # `mark_succeeded` returns False and we MUST NOT commit `completed` to
        # the read model. Otherwise a stale worker would expose a render for a
        # job that another retry has already taken over (or that oncall sees as
        # `dead`), and the asset would race against the retry's own write.
        async with AsyncSessionLocal() as session:
            repo = SqlAlchemyOutfitVisualRepository(session)
            if rendered is None:
                await repo.update_status(asset_id, "rejected_by_quality_gate")
            else:
                image_url, object_key = rendered
                await repo.update_status(asset_id, "completed", image_url=image_url, object_key=object_key)
            ok = await mark_succeeded(session, job.id, claimed_attempt=job.attempts)
            if not ok:
                await session.rollback()
                logger.warning(
                    f"{log_prefix}.lease_lost_discarding_completion",
                    job_id=str(job.id),
                    asset_id=str(asset_id),
                )
                return
            await session.commit()
            if rendered is None:
                logger.info(f"{log_prefix}_rejected_by_quality_gate", asset_id=str(asset_id))
            else:
                logger.info(f"{log_prefix}_generated", asset_id=str(asset_id))
    finally:
        structlog.contextvars.unbind_contextvars("correlation_id", "job_id", "asset_id")


async def reconcile_dead_visual_assets(job_type: str, log_prefix: str) -> int:
    """Flip `OutfitVisualAsset.generation_status` to 'failed' for assets whose
    backing job has gone `dead` via lease recovery (which happens when a worker
    crashes hard enough to never reach `mark_failed` itself).

    Without this, a hard-killed render leaves the asset stuck on `processing`
    forever — oncall sees the job as `dead` but the user-facing GET endpoint
    still says "still rendering."

    Runs as an arq cron alongside `recover_async_job_leases` so the user-visible
    read model converges within one recovery interval."""
    from sqlalchemy import select

    from app.infrastructure.persistence.models.outfit_visual import OutfitVisualAsset
    from app.infrastructure.persistence.repositories.outfit_visual_repo import (
        SqlAlchemyOutfitVisualRepository,
    )

    async with AsyncSessionLocal() as session:
        # All async_jobs of this type that are dead but whose target asset is
        # still in `queued` / `processing`.
        rows = (
            await session.execute(
                select(AsyncJob.id, AsyncJob.target_id, AsyncJob.last_error)
                .join(OutfitVisualAsset, OutfitVisualAsset.id == AsyncJob.target_id)
                .where(
                    AsyncJob.job_type == job_type,
                    AsyncJob.status == "dead",
                    AsyncJob.target_id.is_not(None),
                    OutfitVisualAsset.generation_status.in_(("queued", "processing")),
                )
            )
        ).all()

        if not rows:
            return 0

        repo = SqlAlchemyOutfitVisualRepository(session)
        for _job_id, target_id, last_error in rows:
            await repo.update_status(target_id, "failed", error=last_error or "job_dead_letter")
        await session.commit()
    logger.warning(f"{log_prefix}.reconciled_dead_assets", count=len(rows))
    return len(rows)


async def drain_visual_jobs(
    job_type: str,
    render_fn: RenderFn,
    max_per_tick: int,
    log_prefix: str,
    asset_not_found_event: str,
) -> int:
    processed = 0
    for _ in range(max_per_tick):
        async with AsyncSessionLocal() as session:
            job = await claim_one(session, [job_type])
        if job is None:
            break
        await process_visual_asset_job(
            job,
            job_type=job_type,
            render_fn=render_fn,
            log_prefix=log_prefix,
            asset_not_found_event=asset_not_found_event,
        )
        processed += 1
    if processed:
        logger.info(f"{log_prefix}.drain_completed", processed=processed)
    return processed
