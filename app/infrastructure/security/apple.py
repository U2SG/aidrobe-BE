from typing import Any, cast

import httpx
import jwt

from app.application.auth.ports.apple import AppleIdentity
from app.config.settings import Settings

APPLE_ISSUER = "https://appleid.apple.com"
APPLE_JWKS_URL = "https://appleid.apple.com/auth/keys"


class AppleIdTokenVerifier:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._jwks: dict[str, Any] | None = None

    async def verify(self, id_token: str) -> AppleIdentity:
        if not self.settings.APPLE_CLIENT_ID:
            raise ValueError("APPLE_CLIENT_ID is required")

        if not self.settings.AUTH_APPLE_VERIFY_SIGNATURE:
            payload = jwt.decode(id_token, options={"verify_signature": False})
            self._validate_claims(payload)
            return AppleIdentity(subject=str(payload["sub"]), email=payload.get("email"))

        headers = jwt.get_unverified_header(id_token)
        key_id = headers.get("kid")
        if not isinstance(key_id, str):
            raise ValueError("Apple token missing key id")

        jwk = await self._get_jwk(key_id)
        key = jwt.PyJWK.from_dict(jwk).key
        payload = jwt.decode(
            id_token,
            key=key,
            algorithms=[headers.get("alg", "RS256")],
            audience=self.settings.APPLE_CLIENT_ID,
            issuer=APPLE_ISSUER,
        )
        self._validate_claims(payload)
        return AppleIdentity(subject=str(payload["sub"]), email=payload.get("email"))

    async def _get_jwk(self, key_id: str) -> dict[str, Any]:
        if self._jwks is None:
            async with httpx.AsyncClient(timeout=self.settings.APPLE_JWKS_TIMEOUT_SECONDS) as client:
                response = await client.get(APPLE_JWKS_URL)
                response.raise_for_status()
                self._jwks = response.json()

        for key in self._jwks.get("keys", []):
            if key.get("kid") == key_id:
                return cast(dict[str, Any], key)
        raise ValueError("Apple public key not found")

    def _validate_claims(self, payload: dict[str, Any]) -> None:
        if payload.get("iss") != APPLE_ISSUER:
            raise ValueError("Invalid Apple token issuer")
        if payload.get("aud") != self.settings.APPLE_CLIENT_ID:
            raise ValueError("Invalid Apple token audience")
        if not payload.get("sub"):
            raise ValueError("Apple token subject is required")
