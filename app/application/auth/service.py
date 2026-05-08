from datetime import UTC, datetime, timedelta

from app.application.auth.ports.apple import AppleIdentityVerifier
from app.application.auth.tokens import IssuedTokens
from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.config.settings import Settings
from app.infrastructure.persistence.repositories.operation_log_repo import (
    SqlAlchemyOperationLogRepository,
)
from app.infrastructure.persistence.repositories.user_repo import SqlAlchemyUserRepository
from app.infrastructure.security.jwt.tokens import JwtTokenService


class AuthService:
    def __init__(
        self,
        user_repository: SqlAlchemyUserRepository,
        operation_log_repository: SqlAlchemyOperationLogRepository,
        apple_verifier: AppleIdentityVerifier,
        jwt_service: JwtTokenService,
        settings: Settings,
    ) -> None:
        self.user_repository = user_repository
        self.operation_log_repository = operation_log_repository
        self.apple_verifier = apple_verifier
        self.jwt_service = jwt_service
        self.settings = settings

    async def login_with_apple(self, id_token: str) -> IssuedTokens:
        try:
            identity = await self.apple_verifier.verify(id_token)
        except ValueError as exc:
            raise FriendlyException(
                error_codes.UNAUTHORIZED,
                "Invalid Apple identity token",
                status_code=401,
            ) from exc

        existing_user = await self.user_repository.get_by_apple_subject(identity.subject)
        user = existing_user or await self.user_repository.get_or_create_by_apple_subject(
            identity.subject, email=identity.email
        )
        # Phase E1 — auto-reactivate within grace. PIPL respects user
        # right-to-restore, and without this path soft-deleted users have
        # no way back in (auth gate refuses subsequent calls). Hard-purged
        # users (purged_at set) get a fresh row via get_or_create above,
        # so they aren't reactivated — they're a brand-new account that
        # happens to share the Apple sub.
        if user.deactivated_at is not None and user.purged_at is None:
            user.deactivated_at = None
            await self.operation_log_repository.add(
                user.id, "user_reactivate", {"platform": "ios"}
            )
        refresh_token = self.jwt_service.create_refresh_token()
        session = await self.user_repository.create_session(
            user_id=user.id,
            refresh_token_hash=self.jwt_service.hash_token(refresh_token),
            expires_at=datetime.now(UTC) + timedelta(days=self.settings.JWT_REFRESH_TOKEN_DAYS),
        )
        access_token = self.jwt_service.create_access_token(user.id, session.id)
        tokens = IssuedTokens(
            user_id=user.id,
            access_token=access_token,
            refresh_token=refresh_token,
            token_type="bearer",
            expires_in=self.settings.JWT_ACCESS_TOKEN_MINUTES * 60,
        )
        if existing_user is None:
            await self.operation_log_repository.add(user.id, "user_created")
        await self.operation_log_repository.add(user.id, "user_login", {"platform": "ios"})
        return tokens

    async def refresh(self, refresh_token: str) -> IssuedTokens:
        session = await self.user_repository.get_active_session_by_refresh_hash(
            self.jwt_service.hash_token(refresh_token)
        )
        if session is None:
            raise FriendlyException(
                error_codes.UNAUTHORIZED,
                "Invalid refresh token",
                status_code=401,
            )

        access_token = self.jwt_service.create_access_token(session.user_id, session.id)
        tokens = IssuedTokens(
            user_id=session.user_id,
            access_token=access_token,
            refresh_token=refresh_token,
            token_type="bearer",
            expires_in=self.settings.JWT_ACCESS_TOKEN_MINUTES * 60,
        )
        await self.operation_log_repository.add(session.user_id, "user_login", {"source": "refresh"})
        return tokens
