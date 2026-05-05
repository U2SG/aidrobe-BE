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

            # update wardrobe_item_images
            if job.item_id:
                now = datetime.now(UTC)
                for image_type, url in (
                    ("normalized", result.normalized_url),
                    ("thumbnail", result.thumbnail_url),
                    ("cutout", result.cutout_url),
                ):
                    session.add(WardrobeItemImage(
                        item_id=job.item_id,
                        image_type=image_type,
                        image_url=url,
                        created_at=now,
                    ))

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
