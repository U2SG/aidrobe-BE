import uuid
from typing import Protocol


class UserRateLimiter(Protocol):
    def check(self, user_id: uuid.UUID) -> None: ...
