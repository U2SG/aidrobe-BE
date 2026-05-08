"""C3 — outfit board worker integration tests.

Cover:
- enqueue idempotency on (outfit_id, layout_version) and asset-only fallback
- drain happy path: claim → render → upload → asset.completed → mark_succeeded
- render exception → mark_failed; asset stays processing until dead, then failed
- payload-shape validation
- asset_not_found short-circuits
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.outfit_visual import OutfitVisualAsset
from app.infrastructure.persistence.models.user import User
from app.infrastructure.workers import outfit_board_worker
from app.infrastructure.workers.job_runtime import (
    JOB_STATUS_DEAD,
    JOB_STATUS_QUEUED,
    JOB_STATUS_SUCCEEDED,
)
from app.infrastructure.workers.outfit_board_worker import (
    JOB_TYPE,
    drain_outfit_board_jobs,
    enqueue_outfit_board,
)


@pytest_asyncio.fixture
async def patched_session_factory(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Point the worker's module-level `AsyncSessionLocal` at the
    testcontainers DB."""
    engine = create_async_engine(postgres_url, future=True)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(outfit_board_worker, "AsyncSessionLocal", factory)
    try:
        yield factory
    finally:
        await engine.dispose()


class _FakeStorage:
    """Deterministic stand-in for object storage; returns a stable URL keyed
    by the input key so assertions can reason about it."""

    async def put_object(self, key: str, data: bytes, content_type: str) -> str:
        return f"https://fake-storage/{key}"


def _patch_storage(monkeypatch: pytest.MonkeyPatch, storage: Any) -> None:
    monkeypatch.setattr("app.bootstrap.container.object_storage", lambda: storage)


def _patch_renderer_to_skip_io(monkeypatch: pytest.MonkeyPatch) -> None:
    """Skip the heavy PIL render path — we don't need actual JPEG bytes to
    prove the worker's state-machine semantics."""

    async def _fake_render(
        snapshot: outfit_board_worker._AssetSnapshot,
    ) -> tuple[str, str]:
        from app.bootstrap.container import object_storage

        storage = object_storage()
        key = f"{snapshot.user_id}/outfit_boards/{snapshot.id}.jpg"
        url = await storage.put_object(key, b"\xff\xd8\xff\xe0", "image/jpeg")
        return url, key

    monkeypatch.setattr(outfit_board_worker, "_render_and_upload", _fake_render)


async def _seed_asset(
    factory: async_sessionmaker[AsyncSession],
    *,
    input_snapshot: dict[str, Any] | None = None,
) -> tuple[uuid.UUID, uuid.UUID]:
    user_id = uuid.uuid4()
    async with factory() as s:
        s.add(User(id=user_id))
        # Flush user before child to guarantee FK-safe insert ordering.
        await s.flush()
        asset = OutfitVisualAsset(
            user_id=user_id,
            generation_status="queued",
            visual_type="outfit_board",
            input_snapshot=input_snapshot or {"item_list": []},
        )
        s.add(asset)
        await s.flush()
        asset_id = asset.id
        await s.commit()
    return user_id, asset_id


async def test_enqueue_idempotent_per_outfit_and_layout(
    patched_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id, asset_id = await _seed_asset(patched_session_factory)
    outfit_id = uuid.uuid4()

    async with patched_session_factory() as s:
        first, inserted_first = await enqueue_outfit_board(
            s,
            asset_id=asset_id,
            layout_version="v1",
            outfit_id=outfit_id,
            user_id=user_id,
        )
        second, inserted_second = await enqueue_outfit_board(
            s,
            asset_id=asset_id,
            layout_version="v1",
            outfit_id=outfit_id,
            user_id=user_id,
        )
        await s.commit()

    assert inserted_first is True
    assert inserted_second is False
    assert first.id == second.id

    # Different layout_version → separate job.
    async with patched_session_factory() as s:
        third, inserted_third = await enqueue_outfit_board(
            s,
            asset_id=asset_id,
            layout_version="v2",
            outfit_id=outfit_id,
            user_id=user_id,
        )
        await s.commit()
    assert inserted_third is True
    assert third.id != first.id


async def test_enqueue_falls_back_to_asset_key_when_no_outfit(
    patched_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id, asset_id = await _seed_asset(patched_session_factory)
    async with patched_session_factory() as s:
        job, inserted = await enqueue_outfit_board(
            s,
            asset_id=asset_id,
            layout_version="v1",
            outfit_id=None,
            user_id=user_id,
        )
        await s.commit()
    assert inserted is True
    assert job.idempotency_key == f"outfit_board:asset:{asset_id}"


async def test_drain_renders_and_marks_succeeded(
    patched_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_storage(monkeypatch, _FakeStorage())
    _patch_renderer_to_skip_io(monkeypatch)

    user_id, asset_id = await _seed_asset(patched_session_factory)
    async with patched_session_factory() as s:
        job, _ = await enqueue_outfit_board(
            s,
            asset_id=asset_id,
            layout_version="v1",
            outfit_id=uuid.uuid4(),
            user_id=user_id,
        )
        await s.commit()
        job_id = job.id

    processed = await drain_outfit_board_jobs(ctx=None)
    assert processed == 1

    async with patched_session_factory() as s:
        finalized = await s.get(AsyncJob, job_id)
        assert finalized is not None
        assert finalized.status == JOB_STATUS_SUCCEEDED
        assert finalized.lease_expires_at is None

        asset = await s.get(OutfitVisualAsset, asset_id)
        assert asset is not None
        assert asset.generation_status == "completed"
        assert asset.image_url is not None
        assert asset.image_url.endswith(f"{asset_id}.jpg")


async def test_drain_marks_failed_and_keeps_asset_processing_until_dead(
    patched_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Render exception on first attempt: job goes back to queued; the read
    model stays at 'processing' (not 'failed') so a successful retry can
    still complete it. Only on dead does the asset land on 'failed'."""

    async def _exploding_render(snapshot: outfit_board_worker._AssetSnapshot) -> str:
        raise RuntimeError("S3 down")

    monkeypatch.setattr(outfit_board_worker, "_render_and_upload", _exploding_render)

    user_id, asset_id = await _seed_asset(patched_session_factory)
    async with patched_session_factory() as s:
        from app.infrastructure.workers.job_runtime import enqueue

        job, _ = await enqueue(
            s,
            job_type=JOB_TYPE,
            idempotency_key=f"outfit_board:test:{asset_id}",
            payload={
                "asset_id": str(asset_id),
                "layout_version": "v1",
                "outfit_id": None,
            },
            user_id=user_id,
            target_id=asset_id,
            max_attempts=2,
        )
        await s.commit()
        job_id = job.id

    # 1st drain: queued → running → fail → queued (attempt=1)
    await drain_outfit_board_jobs(ctx=None)
    async with patched_session_factory() as s:
        row = await s.get(AsyncJob, job_id)
        assert row is not None
        assert row.status == JOB_STATUS_QUEUED
        assert row.attempts == 1
        # Asset stays at processing — retry can still succeed.
        asset = await s.get(OutfitVisualAsset, asset_id)
        assert asset is not None
        assert asset.generation_status == "processing"

        # Force backoff into the past so we can re-claim immediately.
        from datetime import UTC, datetime, timedelta

        row.next_run_at = datetime.now(UTC) - timedelta(seconds=1)
        await s.commit()

    # 2nd drain: claim (attempt=2) → fail → dead (attempts == max_attempts)
    await drain_outfit_board_jobs(ctx=None)
    async with patched_session_factory() as s:
        final = await s.get(AsyncJob, job_id)
        assert final is not None
        assert final.status == JOB_STATUS_DEAD
        # Asset now reflects terminal failure.
        asset = await s.get(OutfitVisualAsset, asset_id)
        assert asset is not None
        assert asset.generation_status == "failed"
        assert asset.error_message == "S3 down"


async def test_drain_skips_when_asset_disappeared(
    patched_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stale job pointing at a deleted asset should not crash drain; the job
    is failed with `asset_not_found`."""
    _patch_storage(monkeypatch, _FakeStorage())
    _patch_renderer_to_skip_io(monkeypatch)

    async with patched_session_factory() as s:
        from app.infrastructure.workers.job_runtime import enqueue

        bogus_asset_id = uuid.uuid4()
        job, _ = await enqueue(
            s,
            job_type=JOB_TYPE,
            idempotency_key=f"outfit_board:bogus:{bogus_asset_id}",
            payload={
                "asset_id": str(bogus_asset_id),
                "layout_version": "v1",
                "outfit_id": None,
            },
        )
        await s.commit()
        job_id = job.id

    processed = await drain_outfit_board_jobs(ctx=None)
    assert processed == 1

    async with patched_session_factory() as s:
        final = await s.get(AsyncJob, job_id)
        assert final is not None
        assert final.status == JOB_STATUS_QUEUED
        assert final.last_error == "asset_not_found"


async def test_drain_skips_invalid_payload(
    patched_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Missing asset_id must surface as a structured failure, not a crash."""
    from app.infrastructure.workers.job_runtime import enqueue

    async with patched_session_factory() as s:
        job, _ = await enqueue(
            s,
            job_type=JOB_TYPE,
            idempotency_key=f"outfit_board:bad:{uuid.uuid4()}",
            payload={"layout_version": "v1"},  # missing asset_id
        )
        await s.commit()
        job_id = job.id

    processed = await drain_outfit_board_jobs(ctx=None)
    assert processed == 1

    async with patched_session_factory() as s:
        final = await s.get(AsyncJob, job_id)
        assert final is not None
        assert final.status == JOB_STATUS_QUEUED
        assert final.last_error == "missing asset_id in payload"


def test_idempotency_key_with_outfit() -> None:
    outfit_id = uuid.UUID("22222222-2222-2222-2222-222222222222")
    asset_id = uuid.UUID("33333333-3333-3333-3333-333333333333")
    key = outfit_board_worker._idempotency_key(outfit_id=outfit_id, asset_id=asset_id, layout_version="v1")
    assert key == "outfit_board:22222222-2222-2222-2222-222222222222:v1"


def test_idempotency_key_without_outfit_uses_asset() -> None:
    asset_id = uuid.UUID("44444444-4444-4444-4444-444444444444")
    key = outfit_board_worker._idempotency_key(outfit_id=None, asset_id=asset_id, layout_version="v1")
    assert key == "outfit_board:asset:44444444-4444-4444-4444-444444444444"


def test_drain_constants_are_sane() -> None:
    assert 1 <= outfit_board_worker.MAX_DRAIN_PER_TICK <= 50
    assert outfit_board_worker.JOB_TYPE == "outfit_board"
