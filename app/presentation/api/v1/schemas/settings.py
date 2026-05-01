from datetime import time

from pydantic import BaseModel


class UserSettingsResponse(BaseModel):
    departure_time: time | None = None


class UpdateUserSettingsRequest(BaseModel):
    departure_time: time | None = None
