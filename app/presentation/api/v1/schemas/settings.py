from datetime import time

from pydantic import BaseModel, Field


class UserSettingsResponse(BaseModel):
    departure_time: time | None = None
    push_enabled: bool | None = None
    device_token: str | None = None
    location: str | None = None


class UpdateUserSettingsRequest(BaseModel):
    departure_time: time | None = None
    push_enabled: bool | None = None
    device_token: str | None = Field(default=None, max_length=255)
    location: str | None = Field(default=None, max_length=120)

    def is_set(self, field: str) -> bool:
        return field in self.model_fields_set
