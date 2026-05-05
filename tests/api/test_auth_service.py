from sqlalchemy.ext.asyncio import AsyncSession

from app.application.auth.ports.apple import AppleIdentity
from app.application.auth.service import AuthService
from app.config.settings import Settings
from app.infrastructure.persistence.repositories.operation_log_repo import (
    SqlAlchemyOperationLogRepository,
)
from app.infrastructure.persistence.repositories.user_repo import SqlAlchemyUserRepository
from app.infrastructure.security.jwt.tokens import JwtTokenService


class FakeAppleVerifier:
    async def verify(self, id_token: str) -> AppleIdentity:
        return AppleIdentity(subject=f"apple-{id_token}", email="user@example.com")


async def test_apple_login_issues_tokens_and_refresh_reuses_session(
    db_session: AsyncSession,
) -> None:
    settings = Settings(
        APP_ENV="prod",
        JWT_SECRET="test-secret-with-at-least-32-bytes",
        APPLE_CLIENT_ID="com.example.aidrobe",
    )
    service = AuthService(
        SqlAlchemyUserRepository(db_session),
        SqlAlchemyOperationLogRepository(db_session),
        FakeAppleVerifier(),
        JwtTokenService(settings),
        settings,
    )

    tokens = await service.login_with_apple("identity-token")
    refreshed = await service.refresh(tokens.refresh_token)

    assert tokens.user_id == refreshed.user_id
    assert refreshed.refresh_token == tokens.refresh_token
    assert refreshed.access_token
