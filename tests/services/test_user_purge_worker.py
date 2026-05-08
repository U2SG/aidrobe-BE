"""Phase E1 — user_purge worker integration tests."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.common.enums import Category, ItemStatus
from app.config.settings import Settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.audit_log import AuditLog
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.workers import user_purge_worker
from app.infrastructure.workers.job_runtime import enqueue
from app.infrastructure.workers.user_purge_worker import (
    JOB_TYPE,
    drain_user_purge_jobs,
)


@pytest_asyncio.fixture
async def patched_session_factory(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(postgres_url, future=True)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False
    )
    monkeypatch.setattr(user_purge_worker, "AsyncSessionLocal", factory)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.fixture(autouse=True)
def enable_purge_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default-on for these tests so the flag-gated short-circuit doesn't
    skip the cascade we're trying to verify."""

    def _settings() -> Settings:
        return Settings(APP_ENV="dev", ENABLE_USER_PURGE=True)

    monkeypatch.setattr("app.infrastructure.workers.user_purge_worker.get_settings", _settings)


async def _seed_deactivated_user(
    factory: async_sessionmaker[AsyncSession],
) -> uuid.UUID:
    user_id = uuid.uuid4()
    async with factory() as s:
        s.add(
            User(
                id=user_id,
                email="x@example.com",
                deactivated_at=datetime.now(UTC) - timedelta(days=31),
            )
        )
        s.add(
            WardrobeItem(
                user_id=user_id,
                title="t",
                category=Category.TOP.value,
                color_main="white",
                status=ItemStatus.WEARABLE.value,
            )
        )
        await s.commit()
    return user_id


async def _enqueue_purge_due_now(
    factory: async_sessionmaker[AsyncSession], user_id: uuid.UUID
) -> uuid.UUID:
    """Enqueue with next_run_at set to now-1s so claim_one picks it immediately,
    bypassing the production grace window."""
    async with factory() as s:
        job, _ = await enqueue(
            s,
            job_type=JOB_TYPE,
            idempotency_key=f"user_purge:{user_id}",
            payload={"user_id": str(user_id)},
            user_id=user_id,
            target_id=user_id,
        )
        job.next_run_at = datetime.now(UTC) - timedelta(seconds=1)
        await s.commit()
        return job.id


async def test_drain_purges_deactivated_user_and_cascades(
    patched_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """Hard purge deletes user → CASCADE wipes wardrobe_items + sessions."""
    user_id = await _seed_deactivated_user(patched_session_factory)
    job_id = await _enqueue_purge_due_now(patched_session_factory, user_id)

    processed = await drain_user_purge_jobs(ctx=None)
    assert processed == 1

    async with patched_session_factory() as s:
        # User row gone.
        assert await s.get(User, user_id) is None
        # Wardrobe items cascaded.
        items = (
            await s.execute(select(WardrobeItem).where(WardrobeItem.user_id == user_id))
        ).scalars().all()
        assert items == []
        # Job marked succeeded.
        finalized = await s.get(AsyncJob, job_id)
        assert finalized is not None
        assert finalized.status == "succeeded"
        # audit_log entry recorded WITHOUT cascade-deleting (no FK on user_id).
        audit_rows = (
            await s.execute(
                select(AuditLog).where(
                    AuditLog.user_id == user_id, AuditLog.action == "user.purge"
                )
            )
        ).scalars().all()
        assert len(audit_rows) == 1
        assert audit_rows[0].actor == "system"


async def test_drain_skips_purge_when_user_reactivated(
    patched_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A user who came back during grace must NOT be purged. The job
    finalizes succeeded and a `user.purge_cancelled` audit row appears."""
    user_id = await _seed_deactivated_user(patched_session_factory)
    # Reactivate: clear deactivated_at.
    async with patched_session_factory() as s:
        u = await s.get(User, user_id)
        assert u is not None
        u.deactivated_at = None
        await s.commit()

    await _enqueue_purge_due_now(patched_session_factory, user_id)
    await drain_user_purge_jobs(ctx=None)

    async with patched_session_factory() as s:
        u = await s.get(User, user_id)
        # Still alive.
        assert u is not None
        assert u.deactivated_at is None
        cancel_audits = (
            await s.execute(
                select(AuditLog).where(
                    AuditLog.user_id == user_id,
                    AuditLog.action == "user.purge_cancelled",
                )
            )
        ).scalars().all()
        assert len(cancel_audits) == 1


async def test_drain_idempotent_on_missing_user(
    patched_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """If the user row is already gone (e.g. a previous successful purge
    that was retried by lease recovery), the worker treats it as success
    rather than failing and exhausting attempts."""
    bogus_user_id = uuid.uuid4()
    job_id = await _enqueue_purge_due_now(patched_session_factory, bogus_user_id)
    await drain_user_purge_jobs(ctx=None)

    async with patched_session_factory() as s:
        finalized = await s.get(AsyncJob, job_id)
        assert finalized is not None
        assert finalized.status == "succeeded"


async def test_drain_does_not_run_when_kill_switch_off(
    patched_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The kill switch on `ENABLE_USER_PURGE` lets oncall stop hard purges
    mid-incident. The job goes back to queued (not dead) so flipping the
    flag back on resumes work without losing the queue."""

    def _disabled_settings() -> Settings:
        return Settings(APP_ENV="dev", ENABLE_USER_PURGE=False)

    monkeypatch.setattr(
        "app.infrastructure.workers.user_purge_worker.get_settings", _disabled_settings
    )

    user_id = await _seed_deactivated_user(patched_session_factory)
    job_id = await _enqueue_purge_due_now(patched_session_factory, user_id)
    await drain_user_purge_jobs(ctx=None)

    async with patched_session_factory() as s:
        # Job back in queued (mark_failed with backoff).
        row = await s.get(AsyncJob, job_id)
        assert row is not None
        assert row.status == "queued"
        assert row.last_error == "user_purge_disabled_by_flag"
        # User NOT purged.
        u = await s.get(User, user_id)
        assert u is not None
