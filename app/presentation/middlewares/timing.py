from time import perf_counter

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = structlog.get_logger(__name__)

_HEADER_NAME = b"x-process-time-ms"


class TimingMiddleware:
    """ASGI middleware: measure request duration, inject X-Process-Time-Ms
    into the response (including error responses), and emit a single
    structlog event per completed request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        started_at = perf_counter()
        status_code: int = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message.get("status", 500))
                duration_ms = int((perf_counter() - started_at) * 1000)
                headers = list(message.get("headers", []))
                headers = [(n, v) for n, v in headers if n != _HEADER_NAME]
                headers.append((_HEADER_NAME, str(duration_ms).encode("latin-1")))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            duration_ms = int((perf_counter() - started_at) * 1000)
            logger.info(
                "request.completed",
                duration_ms=duration_ms,
                path=scope.get("path", ""),
                method=scope.get("method", ""),
                status_code=status_code,
            )
