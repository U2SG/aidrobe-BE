"""Generate optional wardrobe display images from trusted garment photos."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from time import monotonic
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.paid_generation_admission import settle_terminal_reservations
from app.application.ports.image_cutout import CutoutResult
from app.common.enums import CutoutStatus, ImageType
from app.config.settings import get_settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.wardrobe import WardrobeItem, WardrobeItemImage
from app.infrastructure.persistence.session import AsyncSessionLocal
from app.infrastructure.storage.object_key import is_legacy_garment_crop
from app.infrastructure.storage.source_image import load_source_bytes as _load_source_bytes
from app.infrastructure.workers.job_runtime import (
    JOB_STATUS_DEAD,
    JOB_STATUS_FAILED,
    claim_is_current,
    claim_one,
    enqueue,
    mark_failed,
    mark_succeeded,
    release_cancelled_claim,
)

logger = structlog.get_logger(__name__)

JOB_TYPE = "cutout"
MAX_DRAIN_PER_TICK = 3
MAX_ATTEMPTS = 3


def cutout_job_key(item_id: uuid.UUID) -> str:
    return f"cutout:{item_id}"


async def enqueue_item_cutout(
    session: AsyncSession,
    *,
    item_id: uuid.UUID,
    user_id: uuid.UUID,
    source_url: str | None,
    category: str,
    source_key: str | None = None,
    source_version: str | None = None,
    expected_cover_url: str | None = None,
    correlation_id: str | None = None,
    title: str | None = None,
    color: str | None = None,
) -> tuple[AsyncJob, bool]:
    """Enqueue one optional product shot per garment, in the admission transaction."""
    return await enqueue(
        session,
        job_type=JOB_TYPE,
        idempotency_key=cutout_job_key(item_id),
        payload={
            "item_id": str(item_id),
            "source_url": source_url,
            "source_key": source_key,
            "source_version": source_version,
            "expected_cover_url": expected_cover_url,
            "category": category,
            "title": title,
            "color": color,
        },
        user_id=user_id,
        target_id=item_id,
        correlation_id=correlation_id,
        max_attempts=MAX_ATTEMPTS,
    )


def _still_queued(item: WardrobeItem, payload: dict[str, Any]) -> bool:
    if (
        item.deleted_at is not None
        or item.cutout_status != CutoutStatus.QUEUED.value
        or item.embedding_source_version != payload.get("source_version")
    ):
        return False
    if payload.get("source_version") is not None:
        return item.cover_image_url == payload.get("expected_cover_url")
    # Legacy pending work is allowed to finish only while the old item still
    # points to its original photo or the crop saved in that job.
    return item.cover_image_url in (item.original_image_url, payload.get("source_url"))


async def _apply_result(
    session: AsyncSession, item: WardrobeItem, *, cutout_url: str, cutout_key: str
) -> None:
    item.cover_image_url = cutout_url
    item.cutout_status = CutoutStatus.READY.value
    standard = await session.scalar(
        select(WardrobeItemImage)
        .where(
            WardrobeItemImage.item_id == item.id,
            WardrobeItemImage.image_type == ImageType.STANDARD.value,
        )
        .order_by(WardrobeItemImage.created_at.desc(), WardrobeItemImage.id)
        .limit(1)
    )
    if standard is not None:
        standard.image_url = cutout_url
        standard.object_key = cutout_key
    else:
        session.add(
            WardrobeItemImage(
                item_id=item.id,
                image_type=ImageType.STANDARD.value,
                image_url=cutout_url,
                object_key=cutout_key,
                created_at=datetime.now(UTC),
            )
        )


async def _mark_failure(job: AsyncJob, reason: str, provider_cost_cents: int | None) -> None:
    async with AsyncSessionLocal() as session:
        status = await mark_failed(
            session,
            job.id,
            claimed_attempt=job.attempts,
            error=reason,
            result={"outcome": "technical_failure", "provider_cost_cents": provider_cost_cents},
        )
        if status == JOB_STATUS_DEAD:
            await settle_terminal_reservations(session, job_type=JOB_TYPE, job_id=job.id)
        await session.commit()


async def _process_one(job: AsyncJob) -> None:
    from app.bootstrap.container import image_cutout_service, object_storage

    payload = job.payload or {}
    item_id = job.target_id
    source_url = payload.get("source_url")
    source_key = payload.get("source_key")
    if item_id is None or job.user_id is None or not (source_url or source_key):
        await _mark_failure(job, "missing payload fields", None)
        return

    structlog.contextvars.bind_contextvars(
        correlation_id=job.correlation_id or "",
        job_id=str(job.id),
        item_id=str(item_id),
    )
    try:
        item_query = select(WardrobeItem).where(WardrobeItem.id == item_id, WardrobeItem.user_id == job.user_id)
        async with AsyncSessionLocal() as session:
            item = await session.scalar(item_query)
            usable = item is not None and _still_queued(item, payload)

        outcome = CutoutResult("not_called", provider_cost_cents=0)
        cutout_url = cutout_key = None
        legacy_crop = is_legacy_garment_crop(source_key, job.user_id) or is_legacy_garment_crop(source_url, job.user_id)
        if usable and not legacy_crop:
            phase = "source"
            try:
                raw = await _load_source_bytes(source_url, source_key, user_id=job.user_id)
                phase = "provider"
                outcome = await image_cutout_service().extract(
                    raw, payload.get("category") or "other", title=payload.get("title"), color=payload.get("color")
                )
                if outcome.image is not None:
                    phase = "upload"
                    cutout_key = f"users/{job.user_id}/items/{item_id}/cutout/{job.id}/{job.attempts}.png"
                    cutout_url = await object_storage().put_object(cutout_key, outcome.image, "image/png")
            except Exception as exc:
                logger.warning("cutout.render_failed", phase=phase, error_type=type(exc).__name__)
                cost = None if phase == "provider" else outcome.provider_cost_cents
                await _mark_failure(job, f"cutout_{phase}_failed", cost)
                return

        async with AsyncSessionLocal() as session:
            item = await session.scalar(item_query.with_for_update())
            if not await claim_is_current(session, job):
                return
            if item is not None and _still_queued(item, payload):
                if cutout_url is not None and cutout_key is not None:
                    await _apply_result(session, item, cutout_url=cutout_url, cutout_key=cutout_key)
                elif outcome.outcome == "quality_rejected":
                    item.cutout_status = CutoutStatus.REJECTED.value
                else:
                    item.cutout_status = CutoutStatus.SKIPPED.value
            # Generated/rejected images carry the paid outcome even if the item
            # changed during the call. Unknown provider cost stays null.
            await mark_succeeded(
                session,
                job.id,
                claimed_attempt=job.attempts,
                result={"outcome": outcome.outcome, "provider_cost_cents": outcome.provider_cost_cents},
                cost_cents=outcome.provider_cost_cents or 0,
            )
            await settle_terminal_reservations(session, job_type=JOB_TYPE, job_id=job.id)
            await session.commit()
    finally:
        structlog.contextvars.unbind_contextvars("correlation_id", "job_id", "item_id")


async def drain_cutout_jobs(ctx: object) -> int:
    """Drain only as many jobs as can fit in the arq deadline."""
    settings = get_settings()
    deadline = monotonic() + settings.ARQ_JOB_TIMEOUT_SECONDS - 5
    attempt_budget = min(
        settings.IMAGE_CUTOUT_TIMEOUT_SECONDS + settings.VISION_TIMEOUT_SECONDS + 35,
        settings.ARQ_JOB_TIMEOUT_SECONDS - 10,
    )
    processed = 0
    for _ in range(MAX_DRAIN_PER_TICK):
        if deadline - monotonic() < attempt_budget or attempt_budget <= 0:
            break
        async with AsyncSessionLocal() as session:
            job = await claim_one(session, [JOB_TYPE])
        if job is None:
            break
        try:
            async with asyncio.timeout(attempt_budget):
                await _process_one(job)
        except TimeoutError:
            await _mark_failure(job, "cutout_timeout", None)
        except asyncio.CancelledError:
            await release_cancelled_claim(job, AsyncSessionLocal)
            raise
        processed += 1
    if processed:
        logger.info("cutout.drain_completed", processed=processed)
    return processed


async def reconcile_dead_cutout_items(ctx: object) -> int:
    """Converge item status and reservation state after terminal lease recovery."""
    async with AsyncSessionLocal() as session:
        rows = (
            await session.execute(
                select(WardrobeItem, AsyncJob.payload)
                .join(AsyncJob, AsyncJob.target_id == WardrobeItem.id)
                .where(
                    AsyncJob.job_type == JOB_TYPE,
                    AsyncJob.status.in_((JOB_STATUS_DEAD, JOB_STATUS_FAILED)),
                    AsyncJob.user_id == WardrobeItem.user_id,
                    WardrobeItem.cutout_status == CutoutStatus.QUEUED.value,
                    WardrobeItem.deleted_at.is_(None),
                )
                .with_for_update(of=WardrobeItem)
            )
        ).all()
        count = 0
        for item, payload in rows:
            if _still_queued(item, payload or {}):
                item.cutout_status = CutoutStatus.FAILED.value
                count += 1
        await settle_terminal_reservations(session, job_type=JOB_TYPE)
        await session.commit()
    if count:
        logger.warning("cutout.reconciled_dead", count=count)
    return count
