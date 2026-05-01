from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.users.settings import UserSettingsUseCase
from app.bootstrap.container import user_repository
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.deps import get_current_user
from app.presentation.api.v1.schemas.common import ResponseModel
from app.presentation.api.v1.schemas.settings import (
    UpdateUserSettingsRequest,
    UserSettingsResponse,
)

router = APIRouter(prefix="/v1/user", tags=["settings"])


@router.get("/settings", response_model=ResponseModel[UserSettingsResponse])
async def get_user_settings(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ResponseModel[UserSettingsResponse]:
    use_case = UserSettingsUseCase(user_repository(session))
    settings = await use_case.get_settings(current_user.id)
    return ResponseModel(data=UserSettingsResponse(departure_time=settings.departure_time))


@router.patch("/settings", response_model=ResponseModel[UserSettingsResponse])
async def update_user_settings(
    payload: UpdateUserSettingsRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ResponseModel[UserSettingsResponse]:
    use_case = UserSettingsUseCase(user_repository(session))
    settings = await use_case.update_settings(current_user.id, payload.departure_time)
    return ResponseModel(data=UserSettingsResponse(departure_time=settings.departure_time))
