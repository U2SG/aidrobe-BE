import uuid
from typing import Protocol

from app.infrastructure.persistence.models.user import User


class UserRepository(Protocol):
    async def get_by_id(self, user_id: uuid.UUID) -> User | None: ...

    async def get_or_create(self, user_id: uuid.UUID) -> User: ...
