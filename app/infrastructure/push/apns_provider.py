from datetime import UTC, datetime

import httpx
import jwt

from app.config.settings import Settings


class ApnsProvider:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @property
    def is_configured(self) -> bool:
        return all(
            [
                self.settings.APNS_KEY_ID,
                self.settings.APNS_TEAM_ID,
                self.settings.APNS_BUNDLE_ID,
                self.settings.APNS_AUTH_KEY_PATH,
            ]
        )

    async def send_recommendation_ready(self, device_token: str, outfit_count: int) -> None:
        if not self.is_configured:
            return
        message = f"Today has {outfit_count} outfit ideas. Check before leaving."
        await self._send(device_token, message)

    async def _send(self, device_token: str, body: str) -> None:
        auth_token = self._auth_token()
        host = "api.sandbox.push.apple.com" if self.settings.APNS_USE_SANDBOX else "api.push.apple.com"
        url = f"https://{host}/3/device/{device_token}"
        headers = {
            "authorization": f"bearer {auth_token}",
            "apns-topic": str(self.settings.APNS_BUNDLE_ID),
            "apns-push-type": "alert",
            "apns-priority": "10",
        }
        payload = {"aps": {"alert": {"title": "Aidrobe", "body": body}, "sound": "default"}}
        async with httpx.AsyncClient(
            timeout=self.settings.APNS_TIMEOUT_SECONDS,
            http2=True,
        ) as client:
            response = await client.post(url, headers=headers, json=payload)
            response.raise_for_status()

    def _auth_token(self) -> str:
        if not self.settings.APNS_AUTH_KEY_PATH:
            raise ValueError("APNS_AUTH_KEY_PATH is required")
        with open(self.settings.APNS_AUTH_KEY_PATH, encoding="utf-8") as key_file:
            private_key = key_file.read()
        now = datetime.now(UTC)
        return jwt.encode(
            {"iss": self.settings.APNS_TEAM_ID, "iat": int(now.timestamp())},
            private_key,
            algorithm="ES256",
            headers={"kid": self.settings.APNS_KEY_ID},
        )
