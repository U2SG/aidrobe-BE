"""STS token port — Phase E5.

Issues short-lived, narrowly-scoped credentials so the iOS client can upload
directly to OSS without ever holding a permanent AccessKey. The contract:

- Each token is bound to a single bucket prefix `users/{user_id}/<sub>/`.
  Users cannot read each other's data even if a token leaks.
- Tokens carry a TTL (default 15 min). Long enough for chunked uploads of
  large originals; short enough that a leaked token is bounded.
- Action-scoped: write tokens cannot read; read tokens cannot delete.

Implementations:
- `AliyunStsProvider` (prod): backend AssumeRole → returns temp credentials.
- `FakeStsProvider` (test): returns deterministic placeholder credentials so
  endpoints can be exercised without the Aliyun SDK installed.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Literal, Protocol

StsAction = Literal["upload", "read"]


@dataclass(frozen=True)
class StsCredentials:
    """The shape iOS expects back. `region` + `endpoint` are bundled so the
    client can construct the OSS URL without hardcoding them."""

    access_key_id: str
    access_key_secret: str
    security_token: str
    expiration: str  # ISO-8601 UTC
    bucket: str
    region: str
    endpoint: str
    object_key_prefix: str  # users/{user_id}/<sub>/


class StsTokenPort(Protocol):
    async def issue(
        self,
        *,
        user_id: uuid.UUID,
        bucket: str,
        action: StsAction,
        ttl_seconds: int = 900,
    ) -> StsCredentials: ...
