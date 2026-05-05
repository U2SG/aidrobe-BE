from collections import defaultdict, deque
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.common import error_codes
from app.config.settings import Settings


class InMemoryRateLimitMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, settings: Settings) -> None:
        super().__init__(app)
        self.settings = settings
        self.window = timedelta(minutes=1)
        self.global_calls: dict[str, deque[datetime]] = defaultdict(deque)
        self.auth_calls: dict[str, deque[datetime]] = defaultdict(deque)

    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        if request.url.path == "/v1/auth/login/apple":
            key = request.client.host if request.client else "unknown"
            if not self._allow(key, self.auth_calls, self.settings.AUTH_RATE_LIMIT_PER_IP_MINUTE):
                return self._limited()
        else:
            rate_key = request.headers.get("Authorization") or request.headers.get("X-User-Id")
            if rate_key and not self._allow(
                rate_key,
                self.global_calls,
                self.settings.API_RATE_LIMIT_PER_USER_MINUTE,
            ):
                return self._limited()
        return await call_next(request)

    def _allow(self, key: str, store: dict[str, deque[datetime]], limit: int) -> bool:
        now = datetime.now(UTC)
        calls = store[key]
        while calls and now - calls[0] >= self.window:
            calls.popleft()
        if len(calls) >= limit:
            return False
        calls.append(now)
        return True

    def _limited(self) -> Response:
        return Response(
            content=f'{{"code":{error_codes.STATE_CONFLICT},"message":"Rate limit exceeded","data":null}}',
            status_code=429,
            media_type="application/json",
        )
