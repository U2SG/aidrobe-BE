import uuid
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.persistence.models.import_task import ImportTask


class SqlAlchemyImportTaskRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, task: ImportTask) -> ImportTask:
        self.session.add(task)
        await self.session.flush()
        return task

    async def get_for_user(self, task_id: uuid.UUID, user_id: uuid.UUID) -> ImportTask | None:
        return cast(
            ImportTask | None,
            await self.session.scalar(
            select(ImportTask).where(ImportTask.id == task_id, ImportTask.user_id == user_id)
            ),
        )

    async def flush(self) -> None:
        await self.session.flush()
