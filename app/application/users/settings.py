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
        self,
        user_id: uuid.UUID,
        departure_time: time | None = None,
        departure_time_set: bool = False,
        timezone: str | None = None,
        timezone_set: bool = False,
        device_token: str | None = None,
        push_enabled: bool | None = None,
        location: str | None = None,
        locale: str | None = None,
        commute_exposure: str | None = None,
        commute_exposure_set: bool = False,
    ) -> UserSettings:
        settings = await self.user_repository.get_or_create_settings(user_id)
        if departure_time_set:
            settings.departure_time = departure_time
        if timezone_set:
            settings.timezone = timezone
        if device_token is not None:
            settings.device_token = device_token
        if push_enabled is not None:
            settings.push_enabled = push_enabled
        if location is not None:
            settings.location = location
        if locale is not None:
            settings.locale = locale
        if commute_exposure_set:
            # *_set pattern (like departure_time): explicit null clears the value.
            settings.commute_exposure = commute_exposure
        await self.user_repository.flush()
        return settings
