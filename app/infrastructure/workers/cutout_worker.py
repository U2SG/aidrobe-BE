"""Item cutout worker — V4 gpt-image-2 product-shot on `job_runtime`.

After import-confirm creates a `WardrobeItem`, this turns its original photo
into a clean flat-lay product shot (person/room removed) for the wardrobe grid.

Mirrors `embedding_worker` for the async plumbing (idempotent per-item enqueue,
claim → process → finalize with lease fencing). On success it updates the
item's `cover_image_url` + `cutout_status='ready'` and points the `standard`
image row at the cutout object; the dead-letter reconcile flips stuck items to
`failed` so the client stops polling.

Source bytes are read straight from object storage by key when the original is
internal (the SSRF guard blocks our own private-network host), falling back to
an HTTP fetch for external import URLs.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, cast

import httpx
import structlog
from sqlalchemy import CursorResult, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import CutoutStatus, ImageType
from app.infrastructure.importers.link_parser import BROWSER_HEADERS, is_safe_url
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.wardrobe import WardrobeItem, WardrobeItemImage
from app.infrastructure.persistence.session import AsyncSessionLocal
from app.infrastructure.workers.job_runtime import (
    claim_one,
    enqueue,
    mark_failed,
    mark_succeeded,
)

logger = structlog.get_logger(__name__)

JOB_TYPE = "cutout"
# Generative gen API is slow (~35s/img at quality=low) — keep the per-tick
# drain small so a worker tick stays bounded and other queues aren't starved.
MAX_DRAIN_PER_TICK = 3


def _idempotency_key(item_id: uuid.UUID) -> str:
    return f"cutout:{item_id}"


async def enqueue_item_cutout(
    session: AsyncSession,
    *,
    item_id: uuid.UUID,
    user_id: uuid.UUID,
    source_url: str,
    category: str,
    source_key: str | None = None,
    correlation_id: str | None = None,
) -> tuple[AsyncJob, bool]:
    """Idempotent (per-item) enqueue. Caller owns commit and must have already
    inserted the `WardrobeItem` + set its `cutout_status='queued'`."""
    return await enqueue(
        session,
        job_type=JOB_TYPE,
        idempotency_key=_idempotency_key(item_id),
        payload={
            "item_id": str(item_id),
            "source_url": source_url,
            "source_key": source_key,
            "category": category,
        },
        user_id=user_id,
        target_id=item_id,
        correlation_id=correlation_id,
    )


def _key_from_url(url: str, bucket: str) -> str | None:
    """Derive the storage object key from an internal file URL.

    Internal URLs are `<base>/<bucket>/<key>` (S3/MinIO) regardless of which
    host the base used, so splitting on `/<bucket>/` is host-agnostic. Returns
    None for URLs that don't reference our bucket (external import links)."""
    marker = f"/{bucket}/"
    idx = url.find(marker)
    if idx == -1:
        return None
    return url[idx + len(marker) :]


async def _load_source_bytes(source_url: str, source_key: str | None) -> bytes:
    """Read the original image. Prefer reading from our own storage by key
    (no HTTP, no SSRF guard); fall back to an HTTP fetch for external URLs."""
    from app.bootstrap.container import object_storage
    from app.config.settings import get_settings

    storage = object_storage()
    key = source_key
    if key is None:
        bucket = get_settings().S3_BUCKET
        if bucket:
            key = _key_from_url(source_url, bucket)
    if key is not None:
        return await storage.get_object(key)

    # External URL (user-pasted import link) — guard against SSRF.
    if not is_safe_url(source_url):
        raise ValueError(f"unsafe source url blocked: {source_url}")
    async with httpx.AsyncClient(timeout=30, headers=BROWSER_HEADERS) as client:
        resp = await client.get(source_url, follow_redirects=True)
        resp.raise_for_status()
        return resp.content


async def _apply_result(
    session: AsyncSession,
    item_id: uuid.UUID,
    *,
    cutout_url: str,
    cutout_key: str,
) -> None:
    """Point the item's cover + `standard` image row at the finished cutout."""
    now = datetime.now(UTC)
    await session.execute(
        update(WardrobeItem)
        .where(WardrobeItem.id == item_id)
        .values(cover_image_url=cutout_url, cutout_status=CutoutStatus.READY.value)
    )
    standard = (
        await session.execute(
            select(WardrobeItemImage).where(
                WardrobeItemImage.item_id == item_id,
                WardrobeItemImage.image_type == ImageType.STANDARD.value,
            )
        )
    ).scalar_one_or_none()
    if standard is not None:
        standard.image_url = cutout_url
        standard.object_key = cutout_key
    else:
        session.add(
            WardrobeItemImage(
                item_id=item_id,
                image_type=ImageType.STANDARD.value,
                image_url=cutout_url,
                object_key=cutout_key,
                created_at=now,
            )
        )


async def _process_one(job: AsyncJob) -> None:
    from app.bootstrap.container import image_cutout_service, object_storage

    payload: dict[str, Any] = job.payload or {}
    item_id_str = payload.get("item_id")
    source_url = payload.get("source_url")
    category = payload.get("category") or "other"
    source_key = payload.get("source_key")
    if not item_id_str or not source_url:
        async with AsyncSessionLocal() as session:
            await mark_failed(session, job.id, claimed_attempt=job.attempts, error="missing payload fields")
            await session.commit()
        return

    item_id = uuid.UUID(item_id_str)
    structlog.contextvars.bind_contextvars(
        correlation_id=job.correlation_id or "",
        job_id=str(job.id),
        item_id=str(item_id),
    )
    try:
        try:
            raw = await _load_source_bytes(source_url, source_key)
            cutout = await image_cutout_service().extract(raw, category)
            storage = object_storage()
            cutout_key = f"{job.user_id}/items/{item_id}/cutout.png"
            cutout_url = await storage.put_object(cutout_key, cutout, "image/png")
        except Exception as exc:
            logger.warning("cutout.render_failed", error=str(exc))
            async with AsyncSessionLocal() as session:
                await mark_failed(session, job.id, claimed_attempt=job.attempts, error=str(exc))
                await session.commit()
            return

        # Fence the item update on this attempt's claim: a slow gen call can
        # outlive the lease, so if it was recovered + reclaimed elsewhere we
        # must NOT overwrite the item (mark_succeeded returns False → rollback).
        async with AsyncSessionLocal() as session:
            await _apply_result(session, item_id, cutout_url=cutout_url, cutout_key=cutout_key)
            ok = await mark_succeeded(session, job.id, claimed_attempt=job.attempts)
            if not ok:
                await session.rollback()
                logger.warning("cutout.lease_lost_discarding", job_id=str(job.id))
                return
            await session.commit()
    finally:
        structlog.contextvars.unbind_contextvars("correlation_id", "job_id", "item_id")


async def drain_cutout_jobs(ctx: object) -> int:
    """arq entrypoint. Drains up to `MAX_DRAIN_PER_TICK` cutout jobs."""
    processed = 0
    for _ in range(MAX_DRAIN_PER_TICK):
        async with AsyncSessionLocal() as session:
            job = await claim_one(session, [JOB_TYPE])
        if job is None:
            break
        await _process_one(job)
        processed += 1
    if processed:
        logger.info("cutout.drain_completed", processed=processed)
    return processed


async def reconcile_dead_cutout_items(ctx: object) -> int:
    """Flip items to `failed` when their cutout job dead-lettered, so the
    client stops polling for a cutout that will never arrive."""
    async with AsyncSessionLocal() as session:
        dead_item_ids = (
            await session.execute(
                select(AsyncJob.target_id).where(
                    AsyncJob.job_type == JOB_TYPE,
                    AsyncJob.status == "dead",
                    AsyncJob.target_id.is_not(None),
                )
            )
        ).scalars().all()
        if not dead_item_ids:
            return 0
        res = cast(
            CursorResult[Any],
            await session.execute(
                update(WardrobeItem)
                .where(
                    WardrobeItem.id.in_(dead_item_ids),
                    WardrobeItem.cutout_status == CutoutStatus.QUEUED.value,
                )
                .values(cutout_status=CutoutStatus.FAILED.value)
            ),
        )
        await session.commit()
    count = res.rowcount or 0
    if count:
        logger.warning("cutout.reconciled_dead", count=count)
    return count
