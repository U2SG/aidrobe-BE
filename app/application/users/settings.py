import uuid
from datetime import time

from app.infrastructure.persistence.models.user import UserSettings
from app.infrastructure.persistence.repositories.user_repo import SqlAlchemyUserRepository


class UserSettingsUseCase:
    def __init__(self, user_repository: SqlAlchemyUserRepository) -> None:
        self.user_repository = user_repository

    async def get_settings(self, user_id: uuid.UUID) -> UserSettings:
        return await self.user_repository.get_or_create_settings(user_id)

    async def update_settings(
        self, user_id: uuid.UUID, departure_time: time | None
    ) -> UserSettings:
        settings = await self.user_repository.get_or_create_settings(user_id)
        settings.departure_time = departure_time
        await self.user_repository.flush()
        return settings
