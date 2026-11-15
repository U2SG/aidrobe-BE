import asyncio
import threading
from types import SimpleNamespace

import pytest

from app.config.settings import Settings
from app.infrastructure.storage import sdk_io
from app.infrastructure.storage.aliyun_oss import AliyunOssStorage
from app.infrastructure.storage.s3_storage import S3ObjectStorage


@pytest.mark.parametrize("provider", ["s3", "oss"])
async def test_blocked_sdk_body_read_does_not_block_event_loop_or_release_capacity(
    monkeypatch: pytest.MonkeyPatch, provider: str
) -> None:
    loop = asyncio.get_running_loop()
    started = asyncio.Event()
    released = threading.Event()
    closed = asyncio.Event()
    second_started = asyncio.Event()
    monkeypatch.setattr(sdk_io, "_slots", asyncio.Semaphore(1))

    class Body:
        def read(self) -> bytes:
            loop.call_soon_threadsafe(started.set)
            if not released.wait(timeout=3):
                raise AssertionError("event loop did not advance during body read")
            return b"image"

        def close(self) -> None:
            loop.call_soon_threadsafe(closed.set)

    def make_body(key: str) -> Body:
        if key == "second":
            loop.call_soon_threadsafe(second_started.set)
        return Body()

    if provider == "s3":
        monkeypatch.setattr(
            "app.infrastructure.storage.s3_storage.boto3.client",
            lambda *args, **kwargs: SimpleNamespace(get_object=lambda *, Key, Bucket: {"Body": make_body(Key)}),
        )
        storage = S3ObjectStorage(Settings(S3_BUCKET="aidrobe"))
    else:
        monkeypatch.setattr(AliyunOssStorage, "_build_bucket", lambda _: SimpleNamespace(get_object=make_body))
        storage = AliyunOssStorage(
            Settings(
                ALIYUN_OSS_BUCKET="aidrobe",
                ALIYUN_OSS_ENDPOINT="https://oss.example",
                ALIYUN_OSS_ACCESS_KEY_ID="stub",
                ALIYUN_OSS_ACCESS_KEY_SECRET="stub",
            )
        )

    first = asyncio.create_task(storage.get_object("first"))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first

        second = asyncio.create_task(storage.get_object("second"))
        await asyncio.sleep(0)
        assert not second_started.is_set()
        assert sdk_io._slots.locked()

        released.set()
        assert await asyncio.wait_for(second, timeout=1) == b"image"
        assert second_started.is_set()
        assert closed.is_set()
    finally:
        released.set()
