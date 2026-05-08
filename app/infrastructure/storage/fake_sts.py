"""Deterministic STS stand-in for tests / dev.

Returns plausibly-shaped credentials WITHOUT calling Aliyun. The token
format is recognizable (starts with `FAKE-`) so anything that ever sees a
fake token in prod logs lights up immediately."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from app.application.ports.sts_token import StsAction, StsCredentials


class FakeStsProvider:
    region = "cn-hangzhou-fake"
    endpoint = "https://oss-fake.local"

    async def issue(
        self,
        *,
        user_id: uuid.UUID,
        bucket: str,
        action: StsAction,
        ttl_seconds: int = 900,
    ) -> StsCredentials:
        prefix = f"users/{user_id}/{action}/"
        expiration = (datetime.now(UTC) + timedelta(seconds=ttl_seconds)).isoformat()
        # Deterministic so tests can pin against `assert ak == "FAKE-AK-...".
        ak = f"FAKE-AK-{user_id}"
        sk = f"FAKE-SK-{user_id}"
        token = f"FAKE-STS-{user_id}-{action}"
        return StsCredentials(
            access_key_id=ak,
            access_key_secret=sk,
            security_token=token,
            expiration=expiration,
            bucket=bucket,
            region=self.region,
            endpoint=self.endpoint,
            object_key_prefix=prefix,
        )
