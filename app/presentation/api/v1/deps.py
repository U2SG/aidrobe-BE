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
        # Phase E1 — soft-deleted users can't auth. Tokens issued before
        # deactivation become useless on the next request rather than at
        # token expiry, which is a stricter guarantee than "wait for the
        # token to expire naturally."
        if user.deactivated_at is not None:
            raise FriendlyException(
                error_codes.UNAUTHORIZED,
                "Account deactivated",
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
    if user.deactivated_at is not None:
        raise FriendlyException(
            error_codes.UNAUTHORIZED,
            "Account deactivated",
            status_code=401,
        )
    sentry_sdk.set_user({"id": str(user.id)})
    return user


def _parse_admin_ids(raw: str) -> set[uuid.UUID]:
    out: set[uuid.UUID] = set()
    for chunk in raw.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        try:
            out.add(uuid.UUID(chunk))
        except ValueError:
            # Misconfigured entries are ignored rather than crashing every
            # admin request — the missing entry just means that user isn't
            # admin, which is the safer failure mode.
            continue
    return out


async def require_admin_user(
    current_user: Annotated[User, Depends(get_current_user)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> User:
    """Admin-gated dependency. Dev fail-open is deliberate: empty ADMIN_USER_IDS
    in dev means every authenticated user is admin. Empty in prod fails
    closed — the surface is locked off until the operator opts in."""
    admin_ids = _parse_admin_ids(settings.ADMIN_USER_IDS)
    if not admin_ids:
        if settings.APP_ENV == "prod":
            raise FriendlyException(
                error_codes.FORBIDDEN,
                "Admin surface disabled",
                status_code=403,
            )
        return current_user
    if current_user.id not in admin_ids:
        raise FriendlyException(
            error_codes.FORBIDDEN,
            "Admin only",
            status_code=403,
        )
    return current_user
