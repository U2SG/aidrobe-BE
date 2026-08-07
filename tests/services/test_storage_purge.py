"""Storage purge — key collection + worker deletion tests.

Covers the PIPL deletion promise end-to-end at the unit seams:
key collection resolves internal URLs and skips external ones; the
user_purge worker enqueues a storage_purge job carrying those keys;
the storage_purge worker hands them to the storage adapter.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.users.storage_purge import (
    collect_user_object_keys,
    enqueue_storage_purge,
)
from app.common.enums import (
    Category,
    ItemStatus,
    PurchaseCandidateSource,
    PurchaseCandidateStatus,
)
from app.config.settings import Settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.purchase_decision import PurchaseCandidate
from app.infrastructure.persistence.models.style import UserStyleProfile
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem, WardrobeItemImage
from app.infrastructure.workers import storage_purge_worker, user_purge_worker
from app.infrastructure.workers.job_runtime import enqueue
from app.infrastructure.workers.storage_purge_worker import drain_storage_purge_jobs
from app.infrastructure.workers.user_purge_worker import drain_user_purge_jobs

_SETTINGS = Settings(APP_ENV="dev", ENABLE_USER_PURGE=True)
_INTERNAL_BASE = _SETTINGS.LOCAL_STORAGE_BASE_URL  # http://localhost:9000/local


@pytest_asyncio.fixture
async def patched_session_factory(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(postgres_url, future=True)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False
    )
    monkeypatch.setattr(user_purge_worker, "AsyncSessionLocal", factory)
    monkeypatch.setattr(storage_purge_worker, "AsyncSessionLocal", factory)
    monkeypatch.setattr(
        "app.infrastructure.workers.user_purge_worker.get_settings", lambda: _SETTINGS
    )
    try:
        yield factory
    finally:
        await engine.dispose()


class _FakeStorage:
    def __init__(self, fail: bool = False) -> None:
        self.deleted: list[str] = []
        self.swept_prefixes: list[str] = []
        self.fail = fail

    async def delete_objects(self, keys: Sequence[str]) -> None:
        if self.fail:
            raise RuntimeError("storage down")
        self.deleted.extend(keys)

    async def delete_prefix(self, prefix: str) -> int:
        if self.fail:
            raise RuntimeError("storage down")
        self.swept_prefixes.append(prefix)
        return 0


async def _seed_user_with_images(
    factory: async_sessionmaker[AsyncSession],
) -> tuple[uuid.UUID, uuid.UUID]:
    """User + item whose images span all three cases: direct object_key,
    internal URL (resolvable), and external URL (must be skipped)."""
    user_id = uuid.uuid4()
    item_id = uuid.uuid4()
    async with factory() as s:
        s.add(
            User(
                id=user_id,
                email="p@example.com",
                deactivated_at=datetime.now(UTC) - timedelta(days=31),
            )
        )
        # Flush the user first — UserStyleProfile has no relationship() to
        # User, so the unit of work won't order the inserts for us.
        await s.flush()
        s.add(
            WardrobeItem(
                id=item_id,
                user_id=user_id,
                title="t",
                category=Category.TOP.value,
                color_main="white",
                status=ItemStatus.WEARABLE.value,
                cover_image_url=f"{_INTERNAL_BASE}/files/users/{user_id}/wardrobe/c1/cover.jpg",
            )
        )
        s.add(
            WardrobeItemImage(
                item_id=item_id,
                image_url=f"{_INTERNAL_BASE}/files/users/{user_id}/wardrobe/o1/orig.jpg",
                object_key=f"users/{user_id}/wardrobe/o1/orig.jpg",
                created_at=datetime.now(UTC),
            )
        )
        s.add(
            WardrobeItemImage(
                item_id=item_id,
                image_url="https://img.example-cdn.com/external/logo.png",
                created_at=datetime.now(UTC),
            )
        )
        s.add(
            UserStyleProfile(
                user_id=user_id,
                source_type="inspiration_image",
                source_url=f"{_INTERNAL_BASE}/files/users/{user_id}/style/p1/mag.pdf",
                weight=1.0,
            )
        )
        s.add(
            AsyncJob(
                job_type="user_export",
                idempotency_key=f"user_export:{user_id}",
                user_id=user_id,
                result={"object_key": f"users/{user_id}/export/e1.json"},
            )
        )
        # V3-004 — an uploaded purchase-candidate screenshot. Stored as a bare
        # object key with no URL column, so it must be collected directly.
        s.add(
            PurchaseCandidate(
                user_id=user_id,
                source_type=PurchaseCandidateSource.SCREENSHOT.value,
                source_object_key=f"users/{user_id}/purchase/p1/shot.jpg",
                status=PurchaseCandidateStatus.PARSED.value,
            )
        )
        # A link candidate owns no object at all — must contribute no key.
        s.add(
            PurchaseCandidate(
                user_id=user_id,
                source_type=PurchaseCandidateSource.SHARE_LINK.value,
                source_url="https://shop.example.com/coat",
                status=PurchaseCandidateStatus.PARSED.value,
            )
        )
        await s.commit()
    return user_id, item_id


async def test_collect_keys_resolves_internal_and_skips_external(
    patched_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id, _ = await _seed_user_with_images(patched_session_factory)
    async with patched_session_factory() as s:
        keys = await collect_user_object_keys(s, user_id, _SETTINGS)
    assert f"users/{user_id}/wardrobe/c1/cover.jpg" in keys
    assert f"users/{user_id}/wardrobe/o1/orig.jpg" in keys
    # source_url files (learned PDFs etc.) are collected too.
    assert f"users/{user_id}/style/p1/mag.pdf" in keys
    # Export bundle keys come from the job result — clear-data has no prefix
    # sweep, so this is its only path to deleting the pre-clear bundle.
    assert f"users/{user_id}/export/e1.json" in keys
    # V3-004 — an uploaded purchase-candidate screenshot. Row deletion cascades
    # via the FK, but without this the blob would be orphaned in object storage.
    assert f"users/{user_id}/purchase/p1/shot.jpg" in keys
    # A link-sourced candidate owns no internal object.
    assert not any("shop.example.com" in k for k in keys)
    # External CDN URL yields no key — we must never delete third-party objects.
    assert not any("example-cdn" in k or "logo.png" in k for k in keys)
    assert len(keys) == len(set(keys))


async def test_user_purge_enqueues_storage_purge_with_keys(
    patched_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id, _ = await _seed_user_with_images(patched_session_factory)
    async with patched_session_factory() as s:
        job, _ = await enqueue(
            s,
            job_type=user_purge_worker.JOB_TYPE,
            idempotency_key=f"user_purge:{user_id}",
            payload={"user_id": str(user_id)},
            user_id=user_id,
            target_id=user_id,
        )
        job.next_run_at = datetime.now(UTC) - timedelta(seconds=1)
        await s.commit()

    assert await drain_user_purge_jobs(ctx=None) == 1

    async with patched_session_factory() as s:
        assert await s.get(User, user_id) is None
        purge_jobs = (
            await s.execute(
                select(AsyncJob).where(
                    AsyncJob.job_type == storage_purge_worker.JOB_TYPE,
                    AsyncJob.user_id == user_id,
                )
            )
        ).scalars().all()
        assert len(purge_jobs) == 1
        keys = purge_jobs[0].payload["keys"]
        assert f"users/{user_id}/wardrobe/o1/orig.jpg" in keys
        assert f"users/{user_id}/wardrobe/c1/cover.jpg" in keys
        # Hard purge (account gone) is the ONLY path allowed to prefix-sweep.
        assert purge_jobs[0].payload["sweep_prefix"] is True


async def _make_purge_job_due_now(
    factory: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> None:
    """Pull the enqueued job's next_run_at into the past. The column default
    is the DB server's now(); claim_one compares against the Python clock —
    a testcontainer clock slightly ahead of the host makes the fresh job
    unclaimable and the drain flaky."""
    async with factory() as s:
        job = (
            await s.execute(
                select(AsyncJob).where(
                    AsyncJob.job_type == storage_purge_worker.JOB_TYPE,
                    AsyncJob.user_id == user_id,
                )
            )
        ).scalar_one()
        job.next_run_at = datetime.now(UTC) - timedelta(seconds=1)
        await s.commit()


async def test_storage_purge_worker_deletes_and_succeeds(
    patched_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _FakeStorage()
    monkeypatch.setattr(storage_purge_worker, "object_storage", lambda: fake)

    user_id = uuid.uuid4()
    keys = [f"users/{user_id}/wardrobe/a/x.jpg", f"users/{user_id}/wardrobe/b/y.jpg"]
    async with patched_session_factory() as s:
        await enqueue_storage_purge(s, user_id, keys, sweep_prefix=True)
        await s.commit()
    await _make_purge_job_due_now(patched_session_factory, user_id)

    assert await drain_storage_purge_jobs(ctx=None) == 1
    assert fake.deleted == keys
    # Prefix sweep runs after key deletion — catches exports/STS uploads.
    assert fake.swept_prefixes == [f"users/{user_id}/"]


async def test_storage_purge_worker_clear_data_job_does_not_sweep(
    patched_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Clear-data jobs (account still active) must be bounded to the keys
    collected at enqueue time — sweeping the live prefix would delete
    uploads made after the clear request."""
    fake = _FakeStorage()
    monkeypatch.setattr(storage_purge_worker, "object_storage", lambda: fake)

    user_id = uuid.uuid4()
    keys = [f"users/{user_id}/wardrobe/a/x.jpg"]
    async with patched_session_factory() as s:
        await enqueue_storage_purge(s, user_id, keys)  # sweep_prefix defaults False
        await s.commit()
    await _make_purge_job_due_now(patched_session_factory, user_id)

    assert await drain_storage_purge_jobs(ctx=None) == 1
    assert fake.deleted == keys
    assert fake.swept_prefixes == []

    async with patched_session_factory() as s:
        job = (
            await s.execute(
                select(AsyncJob).where(
                    AsyncJob.job_type == storage_purge_worker.JOB_TYPE,
                    AsyncJob.user_id == user_id,
                )
            )
        ).scalar_one()
        assert job.status == "succeeded"


async def test_storage_purge_worker_requeues_on_delete_failure(
    patched_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Delete failure must NOT lose the keys — the job goes back to queued
    (retry with backoff; dead-letter alerting after max attempts)."""
    monkeypatch.setattr(
        storage_purge_worker, "object_storage", lambda: _FakeStorage(fail=True)
    )

    user_id = uuid.uuid4()
    async with patched_session_factory() as s:
        await enqueue_storage_purge(s, user_id, [f"users/{user_id}/wardrobe/a/x.jpg"])
        await s.commit()
    await _make_purge_job_due_now(patched_session_factory, user_id)

    await drain_storage_purge_jobs(ctx=None)

    async with patched_session_factory() as s:
        job = (
            await s.execute(
                select(AsyncJob).where(
                    AsyncJob.job_type == storage_purge_worker.JOB_TYPE,
                    AsyncJob.user_id == user_id,
                )
            )
        ).scalar_one()
        assert job.status == "queued"
        assert job.payload["keys"]  # keys retained for the retry
        assert "storage down" in (job.last_error or "")
