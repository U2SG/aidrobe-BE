from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.auth.service import AuthService
from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.repositories.operation_log_repo import (
    SqlAlchemyOperationLogRepository,
)
from app.infrastructure.persistence.repositories.user_repo import SqlAlchemyUserRepository
from app.infrastructure.persistence.session import get_db_session
from app.infrastructure.security.apple import AppleIdTokenVerifier
from app.infrastructure.security.jwt.tokens import JwtTokenService
from app.presentation.api.v1.schemas.auth import (
    AppleLoginRequest,
    AuthTokenResponse,
    RefreshTokenRequest,
)
from app.presentation.api.v1.schemas.common import ResponseModel

router = APIRouter(prefix="/v1/auth", tags=["auth"])


def build_auth_service(session: AsyncSession, settings: Settings) -> AuthService:
    return AuthService(
        SqlAlchemyUserRepository(session),
        SqlAlchemyOperationLogRepository(session),
        AppleIdTokenVerifier(settings),
        JwtTokenService(settings),
        settings,
    )


@router.post("/login/apple", response_model=ResponseModel[AuthTokenResponse])
async def login_with_apple(
    payload: AppleLoginRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ResponseModel[AuthTokenResponse]:
    tokens = await build_auth_service(session, settings).login_with_apple(payload.id_token)
    return ResponseModel(data=AuthTokenResponse(**tokens.__dict__))


@router.post("/refresh", response_model=ResponseModel[AuthTokenResponse])
async def refresh_token(
    payload: RefreshTokenRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ResponseModel[AuthTokenResponse]:
    tokens = await build_auth_service(session, settings).refresh(payload.refresh_token)
    return ResponseModel(data=AuthTokenResponse(**tokens.__dict__))
