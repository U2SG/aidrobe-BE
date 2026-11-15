"""Bound synchronous object-storage SDK work, including reads of response bodies."""

import asyncio
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial

_WORKERS = 8
_executor = ThreadPoolExecutor(max_workers=_WORKERS, thread_name_prefix="object-storage")
_slots = asyncio.Semaphore(_WORKERS)

CONNECT_TIMEOUT_SECONDS = 3
READ_TIMEOUT_SECONDS = 10


async def run_sdk[T, **P](call: Callable[P, T], *args: P.args, **kwargs: P.kwargs) -> T:
    # Capacity stays occupied until the SDK really returns, even if the
    # awaiting job was cancelled. This also bounds the executor queue.
    await _slots.acquire()
    try:
        future = asyncio.get_running_loop().run_in_executor(_executor, partial(call, *args, **kwargs))
    except BaseException:
        _slots.release()
        raise

    def release(completed: asyncio.Future[T]) -> None:
        _slots.release()
        if not completed.cancelled():
            completed.exception()  # A cancelled waiter may no longer observe a late failure.

    future.add_done_callback(release)
    return await asyncio.shield(future)
