import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.persistence.models.base import JsonDict
from app.infrastructure.persistence.models.operation_log import OperationLog


class SqlAlchemyOperationLogRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(
        self,
        user_id: uuid.UUID,
        event: str,
        properties: JsonDict | None = None,
    ) -> OperationLog:
        log = OperationLog(user_id=user_id, event=event, properties=properties)
        self.session.add(log)
        await self.session.flush()
        return log
