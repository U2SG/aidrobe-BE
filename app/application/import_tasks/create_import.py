import uuid

from app.common.enums import ImportStatus
from app.infrastructure.persistence.models.import_task import ImportTask
from app.infrastructure.persistence.repositories.import_task_repo import (
    SqlAlchemyImportTaskRepository,
)


class CreateImageImportTask:
    def __init__(
        self,
        repository: SqlAlchemyImportTaskRepository,
    ) -> None:
        self.repository = repository

    async def execute(
        self,
        user_id: uuid.UUID,
        input_type: str,
        source_type: str,
        image_url: str | None = None,
        source_url: str | None = None,
    ) -> ImportTask:
        input_payload = {"image_url": image_url, "source_url": source_url}

        return await self.repository.add(
            ImportTask(
                user_id=user_id,
                input_type=input_type,
                source_type=source_type,
                task_status=ImportStatus.QUEUED.value,
                input_payload=input_payload,
            )
        )
