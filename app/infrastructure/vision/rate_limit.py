import uuid
from collections import defaultdict, deque
from datetime import UTC, datetime, timedelta

from app.common import error_codes
from app.common.exceptions import FriendlyException


class InMemoryVisionRateLimiter:
    def __init__(self, max_calls: int, window_seconds: int = 3600) -> None:
        self.max_calls = max_calls
        self.window = timedelta(seconds=window_seconds)
        self._calls: dict[uuid.UUID, deque[datetime]] = defaultdict(deque)

    def check(self, user_id: uuid.UUID) -> None:
        now = datetime.now(UTC)
        calls = self._calls[user_id]
        while calls and now - calls[0] >= self.window:
            calls.popleft()

        if len(calls) >= self.max_calls:
            raise FriendlyException(
                error_codes.STATE_CONFLICT,
                "Vision analysis rate limit exceeded",
                status_code=429,
            )
        calls.append(now)
