from datetime import time

from pydantic import BaseModel


class UserSettingsResponse(BaseModel):
    departure_time: time | None = None
    device_token: str | None = None
    push_enabled: bool | None = None
    location: str | None = None


class UpdateUserSettingsRequest(BaseModel):
    departure_time: time | None = None
    device_token: str | None = None
    push_enabled: bool | None = None
    location: str | None = None

    def is_set(self, field: str) -> bool:
        return field in self.model_fields_set
