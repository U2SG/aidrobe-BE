from typing import Annotated, cast

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.users.settings import UserSettingsUseCase
from app.bootstrap.container import user_repository
from app.common.locale import LocaleSetting
from app.infrastructure.persistence.models.user import User, UserSettings
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.deps import get_current_user
from app.presentation.api.v1.schemas.common import ResponseModel
from app.presentation.api.v1.schemas.settings import (
    CommuteExposure,
    UpdateUserSettingsRequest,
    UserSettingsResponse,
)

router = APIRouter(prefix="/v1/user", tags=["settings"])


def settings_response(settings: UserSettings) -> UserSettingsResponse:
    return UserSettingsResponse(
        departure_time=settings.departure_time,
        push_enabled=settings.push_enabled,
        device_token=settings.device_token,
        location=settings.location,
        locale=cast(LocaleSetting, settings.locale),
        commute_exposure=cast(CommuteExposure | None, settings.commute_exposure),
    )


@router.get(
    "/settings",
    response_model=ResponseModel[UserSettingsResponse],
    response_model_exclude_none=True,
)
async def get_user_settings(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ResponseModel[UserSettingsResponse]:
    use_case = UserSettingsUseCase(user_repository(session))
    settings = await use_case.get_settings(current_user.id)
    return ResponseModel(data=settings_response(settings))


@router.patch(
    "/settings",
    response_model=ResponseModel[UserSettingsResponse],
    response_model_exclude_none=True,
)
async def update_user_settings(
    payload: UpdateUserSettingsRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ResponseModel[UserSettingsResponse]:
    use_case = UserSettingsUseCase(user_repository(session))
    settings = await use_case.update_settings(
        current_user.id,
        departure_time=payload.departure_time,
        departure_time_set=payload.is_set("departure_time"),
        device_token=payload.device_token,
        push_enabled=payload.push_enabled,
        location=payload.location,
        locale=payload.locale,
        commute_exposure=payload.commute_exposure,
    )
    return ResponseModel(data=settings_response(settings))
