"""OOTD poster worker integration tests.

Cover:
- enqueue idempotency (per-asset key)
- drain happy path: claim → real render orchestration (download/mask/gen
  mocked, watermark + storage real-ish) → asset.completed → mark_succeeded
- invalid payload surfaces as a structured failure, not a crash
"""

from __future__ import annotations

import io
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.outfit_visual import OutfitVisualAsset
from app.infrastructure.persistence.models.user import User
from app.infrastructure.workers import ootd_poster_worker
from app.infrastructure.workers.job_runtime import JOB_STATUS_QUEUED, JOB_STATUS_SUCCEEDED
from app.infrastructure.workers.ootd_poster_worker import (
    JOB_TYPE,
    drain_ootd_poster_jobs,
    enqueue_ootd_poster,
)


@pytest_asyncio.fixture
async def patched_session_factory(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(postgres_url, future=True)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(ootd_poster_worker, "AsyncSessionLocal", factory)
    try:
        yield factory
    finally:
        await engine.dispose()


class _FakeStorage:
    async def put_object(self, key: str, data: bytes, content_type: str) -> str:
        return f"https://fake-storage/{key}"


def _png_bytes() -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.new("RGB", (64, 96), (210, 210, 210)).save(buf, "PNG")
    return buf.getvalue()


def _patch_render_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Mock the network/heavy bits but keep the real watermark + storage call
    so the render orchestration (and PIL watermark) is actually exercised.
    image_generation_service is forced to the fake (the dev .env sets
    VISION_PROVIDER=qwen, which would otherwise hit the real wanx API)."""
    from app.infrastructure.vision.fake_image_gen import FakeImageGeneration

    monkeypatch.setattr("app.bootstrap.container.object_storage", lambda: _FakeStorage())
    monkeypatch.setattr(
        "app.bootstrap.container.image_generation_service", lambda: FakeImageGeneration()
    )

    async def _fake_download(url: str) -> bytes:
        return _png_bytes()

    monkeypatch.setattr(ootd_poster_worker, "_download_bytes", _fake_download)
    monkeypatch.setattr(
        "app.infrastructure.image.human_mask.build_subject_mask",
        lambda image_bytes, **kw: _png_bytes(),
    )


async def _seed_asset(
    factory: async_sessionmaker[AsyncSession],
    *,
    input_snapshot: dict[str, Any] | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    user_id = uuid.uuid4()
    async with factory() as s:
        s.add(User(id=user_id))
        await s.flush()
        asset = OutfitVisualAsset(
            user_id=user_id,
            generation_status="queued",
            visual_type="ootd_poster",
            input_snapshot=input_snapshot or {"ootd_image_url": "https://x/selfie.jpg"},
        )
        s.add(asset)
        await s.flush()
        asset_id = asset.id
        await s.commit()
    return user_id, asset_id


async def test_enqueue_idempotent_per_asset(
    patched_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id, asset_id = await _seed_asset(patched_session_factory)
    async with patched_session_factory() as s:
        first, inserted_first = await enqueue_ootd_poster(s, asset_id=asset_id, user_id=user_id)
        second, inserted_second = await enqueue_ootd_poster(s, asset_id=asset_id, user_id=user_id)
        await s.commit()
    assert inserted_first is True
    assert inserted_second is False
    assert first.id == second.id
    assert first.idempotency_key == f"ootd_poster:asset:{asset_id}"


async def test_drain_renders_and_marks_succeeded(
    patched_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_render_deps(monkeypatch)
    user_id, asset_id = await _seed_asset(patched_session_factory)
    async with patched_session_factory() as s:
        job, _ = await enqueue_ootd_poster(s, asset_id=asset_id, user_id=user_id)
        await s.commit()
        job_id = job.id

    processed = await drain_ootd_poster_jobs(ctx=None)
    assert processed == 1

    async with patched_session_factory() as s:
        finalized = await s.get(AsyncJob, job_id)
        assert finalized is not None
        assert finalized.status == JOB_STATUS_SUCCEEDED

        asset = await s.get(OutfitVisualAsset, asset_id)
        assert asset is not None
        assert asset.generation_status == "completed"
        assert asset.image_url is not None
        assert asset.image_url.endswith(f"{asset_id}.jpg")


async def test_drain_skips_invalid_payload(
    patched_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.infrastructure.workers.job_runtime import enqueue

    async with patched_session_factory() as s:
        job, _ = await enqueue(
            s,
            job_type=JOB_TYPE,
            idempotency_key=f"ootd_poster:bad:{uuid.uuid4()}",
            payload={},  # missing asset_id
        )
        await s.commit()
        job_id = job.id

    processed = await drain_ootd_poster_jobs(ctx=None)
    assert processed == 1

    async with patched_session_factory() as s:
        final = await s.get(AsyncJob, job_id)
        assert final is not None
        assert final.status == JOB_STATUS_QUEUED
        assert final.last_error == "missing asset_id in payload"


def test_drain_constants_are_sane() -> None:
    assert 1 <= ootd_poster_worker.MAX_DRAIN_PER_TICK <= 50
    assert ootd_poster_worker.JOB_TYPE == "ootd_poster"
