from datetime import time
from typing import Literal

from pydantic import BaseModel, Field, field_validator

from app.common.local_time import validate_timezone_name
from app.common.locale import LocaleSetting

CommuteExposure = Literal["more_outdoor", "less_outdoor"]


class UserSettingsResponse(BaseModel):
    departure_time: time | None = None
    timezone: str | None = None
    push_enabled: bool | None = None
    device_token: str | None = None
    location: str | None = None
    # BE#30 — UI locale ('zh-CN' | 'en-US' | 'system'); 'system' defers to the
    # product default (zh-CN) at resolution time.
    locale: LocaleSetting | None = None
    commute_exposure: CommuteExposure | None = None


class UpdateUserSettingsRequest(BaseModel):
    departure_time: time | None = None
    timezone: str | None = Field(default=None, max_length=64)
    push_enabled: bool | None = None
    device_token: str | None = Field(default=None, max_length=255)
    location: str | None = Field(default=None, max_length=120)
    locale: LocaleSetting | None = None
    commute_exposure: CommuteExposure | None = None

    def is_set(self, field: str) -> bool:
        return field in self.model_fields_set

    @field_validator("timezone")
    @classmethod
    def validate_timezone(cls, value: str | None) -> str | None:
        return validate_timezone_name(value) if value is not None else None
