import uuid
from dataclasses import dataclass


@dataclass(frozen=True)
class IssuedTokens:
    user_id: uuid.UUID
    access_token: str
    refresh_token: str
    token_type: str
    expires_in: int
