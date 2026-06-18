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

    async def get_for_user(
        self, task_id: uuid.UUID, user_id: uuid.UUID, *, for_update: bool = False
    ) -> ImportTask | None:
        # for_update locks the row so a concurrent confirm blocks until this txn
        # commits, then sees task_status=COMPLETED and gets 409 — without the
        # lock both requests read WAITING_CONFIRM and create duplicate items.
        stmt = select(ImportTask).where(
            ImportTask.id == task_id, ImportTask.user_id == user_id
        )
        if for_update:
            stmt = stmt.with_for_update()
        return cast(ImportTask | None, await self.session.scalar(stmt))

    async def flush(self) -> None:
        await self.session.flush()
