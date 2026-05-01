import uuid
from typing import Annotated

from fastapi import Depends, Header
from sqlalchemy.ext.asyncio import AsyncSession

from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.repositories.user_repo import SqlAlchemyUserRepository
from app.infrastructure.persistence.session import get_db_session


async def get_current_user(
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
    x_user_id: Annotated[str | None, Header(alias="X-User-Id")] = None,
) -> User:
    if settings.APP_ENV == "prod":
        raise FriendlyException(
            error_codes.UNAUTHORIZED,
            "Authentication required",
            status_code=401,
        )

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

    repository = SqlAlchemyUserRepository(session)
    return await repository.get_or_create(user_id)
