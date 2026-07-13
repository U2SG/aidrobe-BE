import hashlib
from datetime import UTC, datetime, timedelta

from app.application.auth.ports.apple import AppleIdentityVerifier
from app.application.auth.tokens import IssuedTokens
from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.config.settings import Settings
from app.infrastructure.persistence.models.user import User
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

    async def _login_subject(
        self,
        subject: str,
        email: str | None,
        platform: str,
    ) -> IssuedTokens:
        """Shared post-identity logic: get-or-create the user, auto-reactivate
        within grace, issue tokens, and write the operation log."""
        existing_user = await self.user_repository.get_by_apple_subject(subject)
        user: User = existing_user or await self.user_repository.get_or_create_by_apple_subject(
            subject, email=email
        )
        # Phase E1 — auto-reactivate within grace. PIPL respects user
        # right-to-restore, and without this path soft-deleted users have
        # no way back in (auth gate refuses subsequent calls). Hard-purged
        # users (purged_at set) get a fresh row via get_or_create above,
        # so they aren't reactivated — they're a brand-new account that
        # happens to share the subject.
        if user.deactivated_at is not None and user.purged_at is None:
            user.deactivated_at = None
            await self.operation_log_repository.add(
                user.id, "user_reactivate", {"platform": platform}
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
        await self.operation_log_repository.add(user.id, "user_login", {"platform": platform})
        return tokens

    async def login_with_apple(self, id_token: str) -> IssuedTokens:
        try:
            identity = await self.apple_verifier.verify(id_token)
        except ValueError as exc:
            raise FriendlyException(
                error_codes.UNAUTHORIZED,
                "Invalid Apple identity token",
                status_code=401,
            ) from exc

        return await self._login_subject(identity.subject, identity.email, "ios")

    async def login_with_invite(self, invite_code: str, device_id: str) -> IssuedTokens:
        """Authenticate using a pre-shared invite code + client device id.

        The code only grants ADMISSION — identity derives from the client's
        persistent `device_id`, so one code shared across several testers
        yields separate accounts, while the same install re-logging (even
        with a rotated code) keeps its account. Codes are validated against
        the comma-separated INVITE_LOGIN_CODES setting; an empty setting
        disables the entire flow.
        """
        valid_codes = {
            c.strip() for c in self.settings.INVITE_LOGIN_CODES.split(",") if c.strip()
        }
        if not valid_codes or invite_code not in valid_codes:
            raise FriendlyException(
                error_codes.UNAUTHORIZED,
                "Invalid or disabled invite code",
                status_code=401,
            )

        # Identity = device, hashed to normalize arbitrary client id formats.
        subject = "invite:" + hashlib.sha256(device_id.encode()).hexdigest()[:32]
        return await self._login_subject(subject, None, "invite")

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
