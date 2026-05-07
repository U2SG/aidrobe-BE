import re
import uuid

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Bounded charset (alphanumeric, dash, underscore) to prevent header injection
# (CRLF) and log poisoning via a hostile X-Correlation-Id.
_VALID_CORRELATION_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_HEADER_NAME = b"x-correlation-id"


class CorrelationIdMiddleware:
    """ASGI middleware: read/generate X-Correlation-Id, bind to structlog,
    and inject into response headers on every path (success AND error)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        client_value: str | None = None
        for name, value in scope.get("headers", []):
            if name == _HEADER_NAME:
                client_value = value.decode("latin-1", errors="ignore")
                break
        if client_value and _VALID_CORRELATION_ID.match(client_value):
            correlation_id = client_value
        else:
            correlation_id = str(uuid.uuid4())

        token_correlation = structlog.contextvars.bind_contextvars(
            correlation_id=correlation_id
        )
        header_bytes = correlation_id.encode("latin-1")

        async def send_wrapper(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                headers = [(n, v) for n, v in headers if n != _HEADER_NAME]
                headers.append((_HEADER_NAME, header_bytes))
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            structlog.contextvars.unbind_contextvars("correlation_id")
            del token_correlation
