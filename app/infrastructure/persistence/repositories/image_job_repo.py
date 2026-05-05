import uuid
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.persistence.models.image_job import ImageProcessingJob


class SqlAlchemyImageJobRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, job: ImageProcessingJob) -> ImageProcessingJob:
        self.session.add(job)
        await self.session.flush()
        return job

    async def get_by_id(self, job_id: uuid.UUID) -> ImageProcessingJob | None:
        return cast(
            ImageProcessingJob | None,
            await self.session.scalar(
                select(ImageProcessingJob).where(ImageProcessingJob.id == job_id)
            ),
        )

    async def update_status(
        self,
        job: ImageProcessingJob,
        status: str,
        result_payload: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> None:
        job.status = status
        if result_payload is not None:
            job.result_payload = result_payload
        if error is not None:
            job.error_message = error
        await self.session.flush()

    async def list_pending(self, limit: int = 50) -> list[ImageProcessingJob]:
        result = await self.session.scalars(
            select(ImageProcessingJob)
            .where(ImageProcessingJob.status == "queued")
            .order_by(ImageProcessingJob.created_at)
            .limit(limit)
        )
        return list(result)
