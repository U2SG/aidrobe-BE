import uuid
from typing import Annotated

import jwt
import sentry_sdk
from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.repositories.user_repo import SqlAlchemyUserRepository
from app.infrastructure.persistence.session import get_db_session
from app.infrastructure.security.jwt.tokens import JwtTokenService


async def get_current_user(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    authorization: Annotated[str | None, Header(alias="Authorization")] = None,
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
) -> User:
    repository = SqlAlchemyUserRepository(session)
    if authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise FriendlyException(
                error_codes.UNAUTHORIZED,
                "Bearer token required",
                status_code=401,
            )
        try:
            claims = JwtTokenService(settings).decode_access_token(token)
        except (jwt.InvalidTokenError, ValueError) as exc:
            raise FriendlyException(
                error_codes.UNAUTHORIZED,
                "Invalid access token",
                status_code=401,
            ) from exc

        token_session = await repository.get_active_session(claims.session_id)
        if token_session is None or token_session.user_id != claims.user_id:
            raise FriendlyException(
                error_codes.UNAUTHORIZED,
                "Invalid access token session",
                status_code=401,
            )
        user = await repository.get_by_id(claims.user_id)
        if user is None:
            raise FriendlyException(
                error_codes.UNAUTHORIZED,
                "Invalid access token subject",
                status_code=401,
            )
        sentry_sdk.set_user({"id": str(user.id)})
        return user

    if settings.APP_ENV == "prod":
        raise FriendlyException(error_codes.UNAUTHORIZED, "Authentication required", status_code=401)

    if not x_user_id:
        raise FriendlyException(
            error_codes.UNAUTHORIZED,
            "X-User-Id header required",
            status_code=401,
        )

    try:
        user_id = uuid.UUID(x_user_id)
    except ValueError as exc:
        raise FriendlyException(
            error_codes.VALIDATION_ERROR,
            "X-User-Id must be a valid UUID",
            status_code=422,
        ) from exc

    user = await repository.get_or_create(user_id)
    sentry_sdk.set_user({"id": str(user.id)})
    return user
