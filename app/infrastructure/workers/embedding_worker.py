"""Compute one current embedding per wardrobe item and model."""

from __future__ import annotations

import asyncio
import hashlib
import uuid

import structlog
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.infrastructure.embedding.clip_embedder import ClipEmbedder
from app.infrastructure.embedding.fake_embedding import FakeEmbedding
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.embedding import ItemEmbedding
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.session import AsyncSessionLocal
from app.infrastructure.storage.source_image import InvalidSourceImage, load_source_bytes, resolve_import_source_key
from app.infrastructure.workers.job_runtime import (
    claim_is_current,
    claim_one,
    enqueue,
    mark_failed,
    mark_succeeded,
    release_cancelled_claim,
)

logger = structlog.get_logger(__name__)

JOB_TYPE = "embedding"
MAX_DRAIN_PER_TICK = 25


def _idempotency_key(item_id: uuid.UUID, model_version: str, source_version: str) -> str:
    digest = hashlib.sha256(f"{model_version}\0{source_version}".encode()).hexdigest()
    return f"embedding:{item_id}:{digest}"


async def enqueue_embedding(
    session: AsyncSession,
    *,
    item_id: uuid.UUID,
    image_url: str | None,
    model_version: str,
    source_version: str,
    image_key: str | None = None,
    user_id: uuid.UUID,
    correlation_id: str | None = None,
) -> tuple[AsyncJob, bool]:
    """Enqueue an immutable source for the current item. Caller owns commit."""
    return await enqueue(
        session,
        job_type=JOB_TYPE,
        idempotency_key=_idempotency_key(item_id, model_version, source_version),
        payload={
            "item_id": str(item_id),
            "image_url": image_url,
            "image_key": image_key,
            "model_version": model_version,
            "source_version": source_version,
        },
        user_id=user_id,
        target_id=item_id,
        correlation_id=correlation_id,
    )


async def _fail(job: AsyncJob, reason: str) -> None:
    async with AsyncSessionLocal() as session:
        await mark_failed(session, job.id, claimed_attempt=job.attempts, error=reason)
        await session.commit()


async def _process_one(job: AsyncJob) -> None:
    from app.bootstrap.container import embedding_service, object_storage

    payload = job.payload or {}
    item_id = job.target_id
    image_url = payload.get("image_url")
    image_key = payload.get("image_key")
    model_version = payload.get("model_version")
    source_version = payload.get("source_version")
    if (
        item_id is None or job.user_id is None
        or not (isinstance(image_url, str) or isinstance(image_key, str))
        or not isinstance(model_version, str)
    ):
        await _fail(job, "missing payload fields")
        return

    structlog.contextvars.bind_contextvars(
        correlation_id=job.correlation_id or "",
        job_id=str(job.id),
        item_id=str(item_id),
        model_version=model_version,
    )
    try:
        current = select(WardrobeItem.id).where(
            WardrobeItem.id == item_id,
            WardrobeItem.user_id == job.user_id,
            WardrobeItem.deleted_at.is_(None),
            WardrobeItem.embedding_source_version == source_version,
        )
        async with AsyncSessionLocal() as session:
            # Legacy jobs do not distinguish a single garment from an outfit photo.
            if not isinstance(source_version, str) or not source_version or await session.scalar(current) is None:
                await _skip(session, job, "obsolete_source")
                return

        try:
            embedder = embedding_service()
            if embedder.model_version != model_version:
                await _fail(job, "model_version mismatch")
                return
            if isinstance(image_key, str) and image_key:
                if isinstance(embedder, ClipEmbedder):
                    raw = await load_source_bytes(image_url, image_key, user_id=job.user_id)
                    vector = await embedder.embed_bytes(raw)
                else:
                    resolve_import_source_key(image_url, job.user_id, get_settings(), expected_key=image_key)
                    vector = await embedder.embed_image(object_storage().sign_render_url(image_key))
            elif model_version != FakeEmbedding.model_version:
                # New imports snapshot external URLs. Legacy rows without a
                # verified object must not be fetched by an embedding provider.
                async with AsyncSessionLocal() as session:
                    await _skip(session, job, "unverified_source")
                return
            elif not isinstance(image_url, str) or not image_url:
                await _fail(job, "missing payload fields")
                return
            else:
                vector = await embedder.embed_image(image_url)
        except InvalidSourceImage:
            async with AsyncSessionLocal() as session:
                await _skip(session, job, "unverified_source")
            return
        except Exception as exc:
            logger.warning("embedding.provider_failed", error_type=type(exc).__name__)
            await _fail(job, "embedding_provider_failed")
            return

        async with AsyncSessionLocal() as session:
            valid = await session.scalar(current.with_for_update()) is not None
            if not await claim_is_current(session, job):
                return
            ok = await mark_succeeded(
                session,
                job.id,
                claimed_attempt=job.attempts,
                result=None if valid else {"outcome": "obsolete_source"},
            )
            if not ok:
                return
            if valid:
                insert_vector = insert(ItemEmbedding).values(
                    item_id=item_id, model_name=model_version, source_version=source_version, vector=vector
                )
                await session.execute(
                    insert_vector.on_conflict_do_update(
                        index_elements=[ItemEmbedding.item_id, ItemEmbedding.model_name],
                        set_={
                            "source_version": insert_vector.excluded.source_version,
                            "vector": insert_vector.excluded.vector,
                        },
                    )
                )
            await session.commit()
    finally:
        structlog.contextvars.unbind_contextvars("correlation_id", "job_id", "item_id", "model_version")


async def _skip(session: AsyncSession, job: AsyncJob, outcome: str) -> None:
    await mark_succeeded(session, job.id, claimed_attempt=job.attempts, result={"outcome": outcome})
    await session.commit()


async def drain_embedding_jobs(ctx: object) -> int:
    """Drain within the arq deadline, leaving unfinished claims recoverable."""
    settings = get_settings()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + settings.ARQ_JOB_TIMEOUT_SECONDS - 5
    attempt_budget = min(settings.CLIP_EMBEDDING_TIMEOUT_SECONDS + 30, settings.ARQ_JOB_TIMEOUT_SECONDS - 10)
    processed = 0
    for _ in range(MAX_DRAIN_PER_TICK):
        if deadline - loop.time() < attempt_budget or attempt_budget <= 0:
            break
        async with AsyncSessionLocal() as session:
            job = await claim_one(session, [JOB_TYPE])
        if job is None:
            break
        try:
            async with asyncio.timeout(attempt_budget):
                await _process_one(job)
        except TimeoutError:
            await _fail(job, "embedding_timeout")
        except asyncio.CancelledError:
            await release_cancelled_claim(job, AsyncSessionLocal)
            raise
        processed += 1
    if processed:
        logger.info("embedding.drain_completed", processed=processed)
    return processed
