"""Bounded best-effort delivery; the approved external collector owns durability."""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable, Sequence

import httpx

from app.common.analytics_delivery import AnalyticsRecord
from app.config.settings import Settings


class AnalyticsPump:
    def __init__(
        self,
        send: Callable[[Sequence[AnalyticsRecord]], Awaitable[int]],
        *,
        enabled: Callable[[], bool],
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.send = send
        self.enabled = enabled
        self.now = now
        self.sleep = sleep
        self.pending: deque[tuple[float, AnalyticsRecord]] = deque()
        self.task: asyncio.Task[None] | None = None
        self.closed = False
        self.delivered = 0
        self.dropped = 0

    def enqueue(self, records: Sequence[AnalyticsRecord]) -> bool:
        if self.closed or not self.enabled() or not records or len(self.pending) + len(records) > 128:
            return False
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return False
        self.pending.extend((self.now(), record) for record in records)
        if self.task is None or self.task.done():
            self.task = loop.create_task(self._run())
        return True

    async def _run(self) -> None:
        while self.pending and not self.closed:
            if not self.enabled():
                self.dropped += len(self.pending)
                self.pending.clear()
                return
            batch = [self.pending.popleft() for _ in range(min(8, len(self.pending)))]
            fresh = [(at, record) for at, record in batch if self.now() - at <= 60]
            self.dropped += len(batch) - len(fresh)
            if not fresh:
                continue
            delivered = False
            for attempt in range(3):
                if self.closed or not self.enabled() or self.now() - fresh[0][0] > 60:
                    break
                status = 0
                try:
                    async with asyncio.timeout(2.5):
                        status = await self.send([record for _, record in fresh])
                except Exception:
                    # No payload/URL/token logging, even on adapter failure.
                    pass
                if 200 <= status < 300:
                    delivered = True
                    break
                if 400 <= status < 500 and status != 429:
                    break
                if attempt < 2:
                    await self.sleep(0.25 * 2**attempt)
            if delivered:
                self.delivered += len(fresh)
            else:
                self.dropped += len(fresh)

    async def flush(self) -> None:
        if self.task is not None:
            await self.task

    async def close(self) -> None:
        self.closed = True
        self.dropped += len(self.pending)
        self.pending.clear()
        if self.task is not None and not self.task.done():
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass


class HttpAnalyticsSink:
    def __init__(self, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.settings = settings
        self.transport = transport

    async def send(self, records: Sequence[AnalyticsRecord]) -> int:
        # Credentials and environment partitioning are transport-only, never
        # fields of an event. Use distinct endpoints/credentials per environment.
        headers: dict[str, str] = {"X-Aidrobe-Environment": self.settings.APP_ENV}
        secret = self.settings.V3_ANALYTICS_COLLECTOR_TOKEN
        if secret is not None:
            headers["Authorization"] = "Bearer " + secret.get_secret_value()
        async with httpx.AsyncClient(
            timeout=2.0,
            follow_redirects=False,
            trust_env=False,
            transport=self.transport,
        ) as client:
            # Ignore the response body: a bad collector cannot fill app memory.
            async with client.stream(
                "POST",
                self.settings.V3_ANALYTICS_COLLECTOR_URL,
                json={"events": list(records)},
                headers=headers,
            ) as response:
                return response.status_code
