from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class AppleIdentity:
    subject: str
    email: str | None = None


class AppleIdentityVerifier(Protocol):
    async def verify(self, id_token: str) -> AppleIdentity: ...
