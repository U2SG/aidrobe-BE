import uuid

import structlog

from app.application.ports.vision import VisionPort
from app.common.enums import ImportStatus
from app.infrastructure.persistence.models.import_task import ImportTask
from app.infrastructure.persistence.repositories.import_task_repo import (
    SqlAlchemyImportTaskRepository,
)

logger = structlog.get_logger(__name__)


class CreateImageImportTask:
    def __init__(
        self,
        repository: SqlAlchemyImportTaskRepository,
        vision: VisionPort,
    ) -> None:
        self.repository = repository
        self.vision = vision

    async def execute(
        self,
        user_id: uuid.UUID,
        source_type: str,
        image_url: str,
    ) -> ImportTask:
        input_payload = {"image_url": image_url}
        try:
            result = await self.vision.analyze_image(image_url)
        except Exception as exc:
            logger.warning("vision_analysis_failed", user_id=str(user_id), error=str(exc))
            return await self.repository.add(
                ImportTask(
                    user_id=user_id,
                    input_type="image",
                    source_type=source_type,
                    task_status=ImportStatus.FAILED.value,
                    input_payload=input_payload,
                    error_code="vision_failed",
                    error_message=str(exc),
                )
            )

        return await self.repository.add(
            ImportTask(
                user_id=user_id,
                input_type="image",
                source_type=source_type,
                task_status=ImportStatus.WAITING_CONFIRM.value,
                input_payload=input_payload,
                candidate_payload={
                    "title": result.title,
                    "category": result.category,
                    "color_main": result.color_main,
                    "confidence_score": result.confidence_score,
                    "original_image_url": image_url,
                },
            )
        )
