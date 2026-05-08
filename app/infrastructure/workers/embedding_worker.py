"""Embedding worker — first vertical slice on top of `job_runtime`.

Contract:
- Producers (e.g. `image_worker` after a cutout is ready) call
  `enqueue_embedding` with the source image URL. Idempotency key is
  `embedding:{item_id}:{model_version}`, so re-uploading or replaying never
  produces a second job for the same item+model pair.
- `drain_embedding_jobs` is the arq entrypoint. It claims one job at a time
  via `job_runtime.claim_one`, runs the embedder, persists `ItemEmbedding`,
  and finalizes through `mark_succeeded` / `mark_failed`. Any exception in
  the embed call lands in `mark_failed` (retry with backoff, then dead).
- A cron tick calls `recover_expired_leases` so a worker that died mid-embed
  doesn't pin its job indefinitely.
"""

from __future__ import annotations

import uuid
from typing import Any

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.embedding import ItemEmbedding
from app.infrastructure.persistence.session import AsyncSessionLocal
from app.infrastructure.workers.job_runtime import (
    claim_one,
    enqueue,
    mark_failed,
    mark_succeeded,
)

logger = structlog.get_logger(__name__)

JOB_TYPE = "embedding"
# Drain at most this many jobs per arq invocation. Keeps a single worker tick
# bounded so other arq jobs aren't starved.
MAX_DRAIN_PER_TICK = 25


def _idempotency_key(item_id: uuid.UUID, model_version: str) -> str:
    return f"embedding:{item_id}:{model_version}"


async def enqueue_embedding(
    session: AsyncSession,
    *,
    item_id: uuid.UUID,
    image_url: str,
    model_version: str,
    image_key: str | None = None,
    user_id: uuid.UUID | None = None,
    correlation_id: str | None = None,
) -> tuple[AsyncJob, bool]:
    """Idempotent enqueue for an embedding job. Caller owns commit.

    `image_key` is the storage object key (preferred — the worker resigns
    the URL at execution time so retry backoff up to 2h doesn't outlive a
    30-min signed URL). `image_url` is kept for external/legacy paths
    where we don't own the object."""
    return await enqueue(
        session,
        job_type=JOB_TYPE,
        idempotency_key=_idempotency_key(item_id, model_version),
        payload={
            "item_id": str(item_id),
            "image_url": image_url,
            "image_key": image_key,
            "model_version": model_version,
        },
        user_id=user_id,
        target_id=item_id,
        correlation_id=correlation_id,
    )


async def _process_one(job: AsyncJob) -> None:
    """Execute one claimed embedding job. Bind correlation_id so every log line
    inside the embedder + persistence path carries it."""
    from app.bootstrap.container import embedding_service

    payload: dict[str, Any] = job.payload or {}
    item_id_str = payload.get("item_id")
    image_url = payload.get("image_url")
    image_key = payload.get("image_key")
    model_version = payload.get("model_version")
    # image_url OR image_key satisfies the requirement (image_key is the
    # production path; image_url remains for legacy / external sources).
    if not item_id_str or not (image_url or image_key) or not model_version:
        async with AsyncSessionLocal() as session:
            await mark_failed(session, job.id, claimed_attempt=job.attempts, error="missing payload fields")
            await session.commit()
        return

    item_id = uuid.UUID(item_id_str)
    structlog.contextvars.bind_contextvars(
        correlation_id=job.correlation_id or "",
        job_id=str(job.id),
        item_id=str(item_id),
        model_version=model_version,
    )
    try:
        embedder = embedding_service()
        # Belt-and-suspenders: if the configured embedder's model_version
        # diverged from what the job was enqueued for, treat as a permanent
        # failure rather than corrupting search with a mismatched vector.
        if getattr(embedder, "model_version", None) != model_version:
            async with AsyncSessionLocal() as session:
                await mark_failed(
                    session,
                    job.id,
                    claimed_attempt=job.attempts,
                    error=(
                        f"model_version mismatch: payload={model_version} "
                        f"current={getattr(embedder, 'model_version', None)}"
                    ),
                )
                await session.commit()
            return

        # Resolve a fresh signed URL from the object_key when present so
        # retry backoff (up to 2h) doesn't outlive the 30-min TTL of a
        # URL stamped at enqueue time. The persisted image_url is only
        # safe to use for EXTERNAL sources (import flow) where it points
        # at a third-party host; for internal storage it's also a signed
        # URL with the same TTL problem we're trying to solve, so we
        # MUST NOT silently fall back to it on signing failure.
        fetch_url: str | None = None
        if image_key:
            from app.bootstrap.container import object_storage

            try:
                fetch_url = object_storage().sign_render_url(image_key)
            except Exception as exc:
                logger.warning("embedding.sign_url_failed", error=str(exc))
                # mark_failed → retry; storage is presumably transient.
                # Don't try image_url here: when image_key is set the
                # url itself is internal-signed and equally TTL-bound.
                async with AsyncSessionLocal() as session:
                    await mark_failed(
                        session,
                        job.id,
                        claimed_attempt=job.attempts,
                        error=f"sign_render_url_failed: {exc}",
                    )
                    await session.commit()
                return
        else:
            # No object_key → external URL from import flow (e.g. user
            # pasted a third-party image link). Use it as-is.
            fetch_url = image_url

        try:
            vector = await embedder.embed_image(fetch_url or "")
        except Exception as exc:
            logger.warning("embedding.embed_failed", error=str(exc))
            async with AsyncSessionLocal() as session:
                await mark_failed(session, job.id, claimed_attempt=job.attempts, error=str(exc))
                await session.commit()
            return

        # Fence the vector write on this attempt's claim: if our lease was
        # recovered while we were calling out to the embedder (the call can
        # easily exceed the 10-minute lease for slow remote providers), then
        # `mark_succeeded` returns False and we MUST NOT persist the vector —
        # another worker may be processing the recovered job, and persisting
        # here would race with theirs and leave a duplicate embedding row.
        async with AsyncSessionLocal() as session:
            session.add(
                ItemEmbedding(
                    item_id=item_id,
                    model_name=model_version,
                    vector=vector,
                )
            )
            ok = await mark_succeeded(session, job.id, claimed_attempt=job.attempts, cost_tokens=0, cost_cents=0)
            if not ok:
                await session.rollback()
                logger.warning("embedding.lease_lost_discarding_vector", job_id=str(job.id))
                return
            await session.commit()
    finally:
        structlog.contextvars.unbind_contextvars("correlation_id", "job_id", "item_id", "model_version")


async def drain_embedding_jobs(ctx: object) -> int:
    """arq entrypoint. Drains up to `MAX_DRAIN_PER_TICK` embedding jobs.

    Each claim/finalize uses its own short transaction so a worker crash
    between calls doesn't poison the next claim. Returns the number of jobs
    processed (succeeded or failed) for observability."""
    processed = 0
    for _ in range(MAX_DRAIN_PER_TICK):
        async with AsyncSessionLocal() as session:
            job = await claim_one(session, [JOB_TYPE])
            # claim_one commits internally, so closing the session here is safe.
        if job is None:
            break
        await _process_one(job)
        processed += 1
    if processed:
        logger.info("embedding.drain_completed", processed=processed)
    return processed
