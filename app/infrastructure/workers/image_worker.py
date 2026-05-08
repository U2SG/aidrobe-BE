import uuid
from datetime import UTC, datetime

import structlog

from app.infrastructure.persistence.session import AsyncSessionLocal

logger = structlog.get_logger(__name__)

MAX_RETRIES = 3


async def process_image_job(ctx: object, job_id: str) -> None:
    from app.config.settings import get_settings
    from app.infrastructure.image.rembg_processor import RembgImageProcessor
    from app.infrastructure.persistence.models.wardrobe import WardrobeItemImage
    from app.infrastructure.persistence.repositories.image_job_repo import SqlAlchemyImageJobRepository

    settings = get_settings()

    async with AsyncSessionLocal() as session:
        repo = SqlAlchemyImageJobRepository(session)
        job = await repo.get_by_id(uuid.UUID(job_id))
        if job is None:
            logger.warning("image_job_not_found", job_id=job_id)
            return
        if job.status not in ("queued", "processing"):
            return

        job.status = "processing"
        await session.flush()

        try:
            processor = RembgImageProcessor(settings)
            result = await processor.process(
                job.source_image_url or "",
                str(job.item_id),
                str(job.user_id),
            )

            # update wardrobe_item_images. Phase E5: object_key is the
            # stable identifier; image_url stays for backwards compat
            # (and so a fallback render path exists if signing fails).
            if job.item_id:
                now = datetime.now(UTC)
                for image_type, url, key in (
                    ("normalized", result.normalized_url, result.normalized_key),
                    ("thumbnail", result.thumbnail_url, result.thumbnail_key),
                    ("cutout", result.cutout_url, result.cutout_key),
                ):
                    session.add(
                        WardrobeItemImage(
                            item_id=job.item_id,
                            image_type=image_type,
                            image_url=url,
                            object_key=key,
                            created_at=now,
                        )
                    )

            await repo.update_status(
                job,
                "completed",
                result_payload={
                    "normalized_url": result.normalized_url,
                    "thumbnail_url": result.thumbnail_url,
                    "cutout_url": result.cutout_url,
                    "quality_score": result.quality_score,
                    "quality_flags": result.quality_flags,
                },
            )

            # Embedding enqueue runs in a SAVEPOINT inside this same session.
            # Three reasons this is the right shape:
            # 1) An enqueue DB error rolls back ONLY the savepoint, leaving the
            #    image-job transaction healthy so the outer commit succeeds.
            # 2) Both writes still commit atomically with the image-job row, so
            #    we never end up with a "completed image, no embedding job"
            #    orphan that would require a reconciler to clean up.
            # 3) On retry of this arq job (status='processing' → still inside
            #    the early-return guard above), the embedding enqueue is
            #    idempotent on (item_id, model_version), so a previous-attempt
            #    enqueue followed by a transient failure is recovered cleanly.
            if job.item_id:
                embed_url = result.cutout_url or result.normalized_url
                if embed_url:
                    try:
                        from app.bootstrap.container import embedding_service
                        from app.infrastructure.workers.embedding_worker import (
                            enqueue_embedding,
                        )

                        embedder = embedding_service()
                        async with session.begin_nested():
                            await enqueue_embedding(
                                session,
                                item_id=job.item_id,
                                image_url=embed_url,
                                model_version=embedder.model_version,
                                user_id=job.user_id,
                                correlation_id=str(job.id),
                            )
                    except Exception as exc:
                        # Savepoint already rolled back; image transaction is
                        # still alive. Embedding pipeline intentionally
                        # tolerates this — image processing's own contract is
                        # intact, and a follow-up image-replace will retry.
                        logger.warning(
                            "embedding_enqueue_failed",
                            item_id=str(job.item_id),
                            error=str(exc),
                        )

            await session.commit()
            logger.info("image_job_completed", job_id=job_id)

        except Exception as exc:
            await session.rollback()
            logger.warning("image_job_failed", job_id=job_id, error=str(exc))
            async with AsyncSessionLocal() as err_session:
                err_repo = SqlAlchemyImageJobRepository(err_session)
                job = await err_repo.get_by_id(uuid.UUID(job_id))
                if job is not None:
                    job.retry_count += 1
                    new_status = "failed" if job.retry_count >= MAX_RETRIES else "queued"
                    await err_repo.update_status(job, new_status, error=str(exc))
                    await err_session.commit()
