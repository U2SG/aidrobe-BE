"""user_export worker — DSAR bundle generation via job_runtime."""

import json
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.user import User, UserSettings
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.workers import user_export_worker
from app.infrastructure.workers.job_runtime import enqueue
from app.infrastructure.workers.user_export_worker import JOB_TYPE, drain_user_export_jobs


@pytest_asyncio.fixture
async def patched_factory(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Point the worker's module-level AsyncSessionLocal at the testcontainers DB."""
    engine = create_async_engine(postgres_url, future=True)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(user_export_worker, "AsyncSessionLocal", factory)
    try:
        yield factory
    finally:
        await engine.dispose()


class _CapturingStorage:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}

    async def put_object(self, key: str, data: bytes, content_type: str) -> str:
        self.objects[key] = data
        return f"https://fake-storage/{key}"


async def test_export_worker_bundles_user_data_and_succeeds(
    patched_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _CapturingStorage()
    monkeypatch.setattr("app.bootstrap.container.object_storage", lambda: storage)

    user_id = uuid.uuid4()
    async with patched_factory() as session:
        session.add(User(id=user_id))
        await session.flush()
        session.add(UserSettings(user_id=user_id, commute_exposure="more_outdoor"))
        session.add(
            WardrobeItem(user_id=user_id, title="Shirt", category="top", color_main="white")
        )
        await session.flush()
        job, _ = await enqueue(
            session,
            job_type=JOB_TYPE,
            idempotency_key=f"user_export:{user_id}:test",
            payload={"user_id": str(user_id)},
            user_id=user_id,
            target_id=user_id,
        )
        job_id = job.id
        await session.commit()

    processed = await drain_user_export_jobs(None)
    assert processed == 1

    async with patched_factory() as session:
        finished = await session.scalar(select(AsyncJob).where(AsyncJob.id == job_id))
    assert finished is not None
    assert finished.status == "succeeded"
    expected_key = f"users/{user_id}/export/{job_id}.json"
    assert finished.result is not None
    assert finished.result["object_key"] == expected_key

    bundle = json.loads(storage.objects[expected_key])
    assert bundle["format"] == "aidrobe.user_export.v1"
    assert bundle["user"]["id"] == str(user_id)
    assert bundle["settings"]["commute_exposure"] == "more_outdoor"
    assert [i["title"] for i in bundle["wardrobe_items"]] == ["Shirt"]
    assert bundle["wear_history"] == []


async def test_export_worker_skips_purged_user(
    patched_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    storage = _CapturingStorage()
    monkeypatch.setattr("app.bootstrap.container.object_storage", lambda: storage)

    ghost_user_id = uuid.uuid4()
    async with patched_factory() as session:
        job, _ = await enqueue(
            session,
            job_type=JOB_TYPE,
            idempotency_key=f"user_export:{ghost_user_id}:test",
            payload={"user_id": str(ghost_user_id)},
            # No users row on purpose — simulates purge racing the export.
            user_id=None,
            target_id=ghost_user_id,
        )
        job_id = job.id
        await session.commit()

    processed = await drain_user_export_jobs(None)
    assert processed == 1

    async with patched_factory() as session:
        finished = await session.scalar(select(AsyncJob).where(AsyncJob.id == job_id))
    assert finished is not None
    # Succeed-with-skip: benign, must not retry into dead-letter paging.
    assert finished.status == "succeeded"
    assert finished.result == {"skipped": "user_purged"}
    assert storage.objects == {}
