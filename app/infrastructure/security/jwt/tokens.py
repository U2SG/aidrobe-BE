import hashlib
import secrets
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

import jwt

from app.config.settings import Settings


@dataclass(frozen=True)
class AccessTokenClaims:
    user_id: uuid.UUID
    session_id: uuid.UUID


class JwtTokenService:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def create_access_token(self, user_id: uuid.UUID, session_id: uuid.UUID) -> str:
        now = datetime.now(UTC)
        payload: dict[str, Any] = {
            "sub": str(user_id),
            "sid": str(session_id),
            "iat": int(now.timestamp()),
            "exp": int(
                (now + timedelta(minutes=self.settings.JWT_ACCESS_TOKEN_MINUTES)).timestamp()
            ),
            "aud": self.settings.JWT_AUDIENCE,
            "iss": self.settings.JWT_ISSUER,
        }
        return jwt.encode(payload, self.settings.JWT_SECRET, algorithm=self.settings.JWT_ALGORITHM)

    def decode_access_token(self, token: str) -> AccessTokenClaims:
        payload = jwt.decode(
            token,
            self.settings.JWT_SECRET,
            algorithms=[self.settings.JWT_ALGORITHM],
            audience=self.settings.JWT_AUDIENCE,
            issuer=self.settings.JWT_ISSUER,
        )
        return AccessTokenClaims(
            user_id=uuid.UUID(str(payload["sub"])),
            session_id=uuid.UUID(str(payload["sid"])),
        )

    def create_refresh_token(self) -> str:
        return secrets.token_urlsafe(48)

    def hash_token(self, token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()
