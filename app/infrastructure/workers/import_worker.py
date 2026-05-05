import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports.link_parser import LinkParserPort
from app.application.ports.rate_limit import UserRateLimiter
from app.application.ports.vision import VisionPort
from app.bootstrap.container import link_parser, vision_rate_limiter, vision_service
from app.common.enums import ImportStatus
from app.infrastructure.persistence.repositories.import_task_repo import (
    SqlAlchemyImportTaskRepository,
)
from app.infrastructure.persistence.session import AsyncSessionLocal

logger = structlog.get_logger(__name__)


class ProcessImageImportTask:
    def __init__(
        self,
        repository: SqlAlchemyImportTaskRepository,
        vision: VisionPort,
        parser: LinkParserPort | None = None,
        vision_rate_limiter: UserRateLimiter | None = None,
    ) -> None:
        self.repository = repository
        self.vision = vision
        self.parser = parser
        self.vision_rate_limiter = vision_rate_limiter

    async def execute(self, task_id: uuid.UUID, user_id: uuid.UUID) -> None:
        task = await self.repository.get_for_user(task_id, user_id)
        if task is None:
            logger.warning("import_task_not_found_for_worker", task_id=str(task_id), user_id=str(user_id))
            return
        if task.task_status != ImportStatus.QUEUED.value:
            return

        task.task_status = ImportStatus.PROCESSING.value
        task.error_code = None
        task.error_message = None
        await self.repository.flush()

        try:
            image_url = task.input_payload.get("image_url")
            source_url = task.input_payload.get("source_url")
            if image_url is None and source_url is not None and self.parser is not None:
                image_url = await self.parser.extract_image_url(source_url)

            if not isinstance(image_url, str) or not image_url:
                raise ValueError("image_url is required")

            if self.vision_rate_limiter is not None:
                self.vision_rate_limiter.check(user_id)
            result = await self.vision.analyze_image(image_url)
        except Exception as exc:
            logger.warning("import_task_processing_failed", task_id=str(task_id), error=str(exc))
            task.task_status = ImportStatus.FAILED.value
            task.error_code = "vision_failed"
            task.error_message = str(exc)
            await self.repository.flush()
            return

        task.task_status = ImportStatus.WAITING_CONFIRM.value
        task.candidate_payload = {
            "title": result.title,
            "category": result.category,
            "color_main": result.color_main,
            "confidence_score": result.confidence_score,
            "original_image_url": image_url,
        }
        await self.repository.flush()


async def process_import_task_with_session(
    session: AsyncSession,
    task_id: uuid.UUID,
    user_id: uuid.UUID,
) -> None:
    worker = ProcessImageImportTask(
        SqlAlchemyImportTaskRepository(session),
        vision_service(),
        link_parser(),
        vision_rate_limiter(),
    )
    await worker.execute(task_id, user_id)


async def process_import_task(ctx: object, task_id: str, user_id: str) -> None:
    """arq-compatible entry point for deployed workers."""
    async with AsyncSessionLocal() as session:
        await process_import_task_with_session(session, uuid.UUID(task_id), uuid.UUID(user_id))
        await session.commit()


class WorkerSettings:
    functions = [process_import_task]
