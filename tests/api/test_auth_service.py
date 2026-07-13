import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.auth.ports.apple import AppleIdentity
from app.application.auth.service import AuthService
from app.common.exceptions import FriendlyException
from app.config.settings import Settings
from app.infrastructure.persistence.repositories.operation_log_repo import (
    SqlAlchemyOperationLogRepository,
)
from app.infrastructure.persistence.repositories.user_repo import SqlAlchemyUserRepository
from app.infrastructure.security.jwt.tokens import JwtTokenService


class FakeAppleVerifier:
    async def verify(self, id_token: str) -> AppleIdentity:
        return AppleIdentity(subject=f"apple-{id_token}", email="user@example.com")


def _make_service(db_session: AsyncSession, extra: dict | None = None) -> AuthService:
    settings = Settings(
        APP_ENV="prod",
        JWT_SECRET="test-secret-with-at-least-32-bytes",
        APPLE_CLIENT_ID="com.example.aidrobe",
        **(extra or {}),
    )
    return AuthService(
        SqlAlchemyUserRepository(db_session),
        SqlAlchemyOperationLogRepository(db_session),
        FakeAppleVerifier(),
        JwtTokenService(settings),
        settings,
    )


async def test_apple_login_issues_tokens_and_refresh_reuses_session(
    db_session: AsyncSession,
) -> None:
    service = _make_service(db_session)

    tokens = await service.login_with_apple("identity-token")
    refreshed = await service.refresh(tokens.refresh_token)

    assert tokens.user_id == refreshed.user_id
    assert refreshed.refresh_token == tokens.refresh_token
    assert refreshed.access_token


async def test_invite_login_valid_code_issues_tokens(db_session: AsyncSession) -> None:
    service = _make_service(db_session, {"INVITE_LOGIN_CODES": "alpha,beta"})

    tokens = await service.login_with_invite("alpha", "device-aaaa-1111")

    assert tokens.user_id
    assert tokens.access_token
    assert tokens.refresh_token


async def test_invite_login_same_device_returns_same_user(db_session: AsyncSession) -> None:
    """Identity derives from device_id — same install keeps its account even
    when the operator rotates the invite code."""
    service = _make_service(db_session, {"INVITE_LOGIN_CODES": "alpha,beta"})

    tokens1 = await service.login_with_invite("alpha", "device-aaaa-1111")
    tokens2 = await service.login_with_invite("beta", "device-aaaa-1111")

    assert tokens1.user_id == tokens2.user_id


async def test_invite_login_same_code_different_devices_get_separate_users(
    db_session: AsyncSession,
) -> None:
    """The code is admission only — one code shared to several testers must
    NOT collapse them into a shared wardrobe."""
    service = _make_service(db_session, {"INVITE_LOGIN_CODES": "alpha"})

    tokens1 = await service.login_with_invite("alpha", "device-aaaa-1111")
    tokens2 = await service.login_with_invite("alpha", "device-bbbb-2222")

    assert tokens1.user_id != tokens2.user_id


async def test_invite_login_wrong_code_raises_401(db_session: AsyncSession) -> None:
    service = _make_service(db_session, {"INVITE_LOGIN_CODES": "alpha"})

    with pytest.raises(FriendlyException) as exc_info:
        await service.login_with_invite("wrong-code", "device-aaaa-1111")

    assert exc_info.value.status_code == 401


async def test_invite_login_empty_codes_raises_401(db_session: AsyncSession) -> None:
    service = _make_service(db_session, {"INVITE_LOGIN_CODES": ""})

    with pytest.raises(FriendlyException) as exc_info:
        await service.login_with_invite("alpha", "device-aaaa-1111")

    assert exc_info.value.status_code == 401
