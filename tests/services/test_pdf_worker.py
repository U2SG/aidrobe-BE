"""M19 — pdf_worker integration tests.

Cover:
- enqueue idempotency on parent_id
- drain happy path: claim → (fake) rasterize → keep pages → child rows created
  via the existing style_learn path → parent learned → mark_succeeded
- blank/ad page filtering
- rasterize failure → mark_failed; parent lands on failed once dead
- payload-shape validation
- at-most-once: a reclaim with children already present skips re-ingest

The PDF rasterizer is faked (we never require poppler); the per-image style
analysis is the real `learn_style_source` (URL heuristic, no network when
source_url is None).
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.style import UserStyleProfile
from app.infrastructure.persistence.models.user import User
from app.infrastructure.workers import pdf_worker
from app.infrastructure.workers.job_runtime import (
    JOB_STATUS_DEAD,
    JOB_STATUS_QUEUED,
    JOB_STATUS_SUCCEEDED,
)
from app.infrastructure.workers.pdf_worker import (
    JOB_TYPE,
    drain_pdf_ingest_jobs,
    enqueue_pdf_ingest,
)


@pytest_asyncio.fixture
async def patched_session_factory(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """Point both the pdf_worker AND style_learn_worker module-level
    `AsyncSessionLocal` at the testcontainers DB (child rows are written through
    style_learn_worker's session)."""
    engine = create_async_engine(postgres_url, future=True)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(pdf_worker, "AsyncSessionLocal", factory)
    from app.infrastructure.workers import style_learn_worker

    monkeypatch.setattr(style_learn_worker, "AsyncSessionLocal", factory)
    try:
        yield factory
    finally:
        await engine.dispose()


class _FakeStorage:
    async def put_object(self, key: str, data: bytes, content_type: str) -> str:
        return f"https://fake-storage/{key}"


def _patch_storage(monkeypatch: pytest.MonkeyPatch, storage: Any) -> None:
    monkeypatch.setattr("app.bootstrap.container.object_storage", lambda: storage)


def _patch_rasterizer(monkeypatch: pytest.MonkeyPatch, pages: list[bytes]) -> None:
    """Replace the poppler-backed rasterizer with a deterministic page list so
    tests never need the real binary."""
    monkeypatch.setattr(pdf_worker, "_rasterize_pdf", lambda _b, dpi=pdf_worker.RENDER_DPI: pages)

    async def _fake_download(_url: str) -> bytes:
        return b"%PDF-1.4 fake"

    monkeypatch.setattr(pdf_worker, "_download_pdf", _fake_download)


async def _seed_parent(factory: async_sessionmaker[AsyncSession]) -> tuple[uuid.UUID, uuid.UUID]:
    user_id = uuid.uuid4()
    async with factory() as s:
        s.add(User(id=user_id))
        await s.flush()
        parent = UserStyleProfile(
            user_id=user_id,
            source_type="pdf_magazine",
            source_url="https://oss.example.com/magazine.pdf",
            weight=1.0,
            learn_status="queued",
        )
        s.add(parent)
        await s.flush()
        parent_id = parent.id
        await s.commit()
    return user_id, parent_id


async def test_enqueue_idempotent_per_parent(
    patched_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _user_id, parent_id = await _seed_parent(patched_session_factory)
    async with patched_session_factory() as s:
        first, inserted_first = await enqueue_pdf_ingest(
            s, parent_id=parent_id, file_url="https://oss.example.com/magazine.pdf"
        )
        second, inserted_second = await enqueue_pdf_ingest(
            s, parent_id=parent_id, file_url="https://oss.example.com/magazine.pdf"
        )
        await s.commit()
    assert inserted_first is True
    assert inserted_second is False
    assert first.id == second.id
    assert first.idempotency_key == f"pdf_ingest:{parent_id}"


async def test_drain_creates_child_rows_and_marks_learned(
    patched_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_storage(monkeypatch, _FakeStorage())
    # Two valid outfit pages + one blank (under MIN_PAGE_BYTES) → 2 kept.
    big = b"x" * (pdf_worker.MIN_PAGE_BYTES + 10)
    blank = b"x" * 10
    _patch_rasterizer(monkeypatch, [big, blank, big])

    user_id, parent_id = await _seed_parent(patched_session_factory)
    async with patched_session_factory() as s:
        job, _ = await enqueue_pdf_ingest(
            s, parent_id=parent_id, file_url="https://oss.example.com/magazine.pdf", user_id=user_id
        )
        await s.commit()
        job_id = job.id

    processed = await drain_pdf_ingest_jobs(ctx=None)
    assert processed == 1

    async with patched_session_factory() as s:
        finalized = await s.get(AsyncJob, job_id)
        assert finalized is not None
        assert finalized.status == JOB_STATUS_SUCCEEDED
        assert finalized.result == {"kept_pages": 2}

        parent = await s.get(UserStyleProfile, parent_id)
        assert parent is not None
        assert parent.learn_status == "learned"
        assert parent.learned_summary == {"kept_pages": 2, "source": "pdf_magazine"}

        child_count = await s.scalar(
            select(func.count()).select_from(UserStyleProfile).where(UserStyleProfile.source_id == parent_id)
        )
        assert child_count == 2
        children = (
            await s.scalars(select(UserStyleProfile).where(UserStyleProfile.source_id == parent_id))
        ).all()
        assert all(c.source_type == "inspiration_image" for c in children)
        # learn_style_source ran on each child → learned, with a summary.
        assert all(c.learn_status == "learned" for c in children)
        assert all(c.image_url is not None for c in children)


async def test_drain_rasterize_failure_marks_failed_then_dead(
    patched_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_storage(monkeypatch, _FakeStorage())

    def _boom(_b: bytes, dpi: int = pdf_worker.RENDER_DPI) -> list[bytes]:
        raise RuntimeError("pdf2image_unavailable: poppler missing")

    monkeypatch.setattr(pdf_worker, "_rasterize_pdf", _boom)

    async def _fake_download(_url: str) -> bytes:
        return b"%PDF-1.4 fake"

    monkeypatch.setattr(pdf_worker, "_download_pdf", _fake_download)

    user_id, parent_id = await _seed_parent(patched_session_factory)
    async with patched_session_factory() as s:
        from app.infrastructure.workers.job_runtime import enqueue

        job, _ = await enqueue(
            s,
            job_type=JOB_TYPE,
            idempotency_key=f"pdf_ingest:test:{parent_id}",
            payload={"parent_id": str(parent_id), "file_url": "https://oss.example.com/magazine.pdf"},
            user_id=user_id,
            target_id=parent_id,
            max_attempts=2,
        )
        await s.commit()
        job_id = job.id

    # 1st drain: queued → running → fail → queued; parent stays processing.
    await drain_pdf_ingest_jobs(ctx=None)
    async with patched_session_factory() as s:
        row = await s.get(AsyncJob, job_id)
        assert row is not None
        assert row.status == JOB_STATUS_QUEUED
        assert row.attempts == 1
        parent = await s.get(UserStyleProfile, parent_id)
        assert parent is not None
        assert parent.learn_status == "processing"

        from datetime import UTC, datetime, timedelta

        row.next_run_at = datetime.now(UTC) - timedelta(seconds=1)
        await s.commit()

    # 2nd drain: claim → fail → dead; parent now failed.
    await drain_pdf_ingest_jobs(ctx=None)
    async with patched_session_factory() as s:
        final = await s.get(AsyncJob, job_id)
        assert final is not None
        assert final.status == JOB_STATUS_DEAD
        parent = await s.get(UserStyleProfile, parent_id)
        assert parent is not None
        assert parent.learn_status == "failed"


async def test_drain_skips_invalid_payload(
    patched_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    from app.infrastructure.workers.job_runtime import enqueue

    async with patched_session_factory() as s:
        job, _ = await enqueue(
            s,
            job_type=JOB_TYPE,
            idempotency_key=f"pdf_ingest:bad:{uuid.uuid4()}",
            payload={"file_url": "https://oss.example.com/x.pdf"},  # missing parent_id
        )
        await s.commit()
        job_id = job.id

    processed = await drain_pdf_ingest_jobs(ctx=None)
    assert processed == 1
    async with patched_session_factory() as s:
        final = await s.get(AsyncJob, job_id)
        assert final is not None
        assert final.last_error == "missing parent_id/file_url in payload"


async def test_drain_at_most_once_when_children_exist(
    patched_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If a prior attempt already created children (lease-recovery reclaim), the
    re-run must NOT rasterize again or duplicate child rows."""
    _patch_storage(monkeypatch, _FakeStorage())

    def _should_not_run(_b: bytes, dpi: int = pdf_worker.RENDER_DPI) -> list[bytes]:
        raise AssertionError("rasterize must be skipped when children already exist")

    monkeypatch.setattr(pdf_worker, "_rasterize_pdf", _should_not_run)

    user_id, parent_id = await _seed_parent(patched_session_factory)
    # Pre-seed one child as if a prior attempt ingested it.
    async with patched_session_factory() as s:
        s.add(
            UserStyleProfile(
                user_id=user_id,
                source_type="inspiration_image",
                source_id=parent_id,
                image_url="https://fake-storage/page_000.jpg",
                weight=1.0,
                learn_status="learned",
            )
        )
        await s.commit()

    async with patched_session_factory() as s:
        job, _ = await enqueue_pdf_ingest(
            s, parent_id=parent_id, file_url="https://oss.example.com/magazine.pdf", user_id=user_id
        )
        await s.commit()
        job_id = job.id

    processed = await drain_pdf_ingest_jobs(ctx=None)
    assert processed == 1
    async with patched_session_factory() as s:
        final = await s.get(AsyncJob, job_id)
        assert final is not None
        assert final.status == JOB_STATUS_SUCCEEDED
        # Still exactly one child — no duplication.
        child_count = await s.scalar(
            select(func.count()).select_from(UserStyleProfile).where(UserStyleProfile.source_id == parent_id)
        )
        assert child_count == 1


def test_idempotency_key() -> None:
    parent_id = uuid.UUID("55555555-5555-5555-5555-555555555555")
    assert pdf_worker._idempotency_key(parent_id) == "pdf_ingest:55555555-5555-5555-5555-555555555555"


def test_blank_page_filter() -> None:
    assert pdf_worker._is_outfit_page(b"x" * (pdf_worker.MIN_PAGE_BYTES + 1)) is True
    assert pdf_worker._is_outfit_page(b"x" * 5) is False


def test_drain_constants_are_sane() -> None:
    assert pdf_worker.JOB_TYPE == "pdf_ingest"
    assert 1 <= pdf_worker.MAX_DRAIN_PER_TICK <= 50
