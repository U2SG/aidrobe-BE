"""Import worker on job_runtime (BE#26).

Cover:
- enqueue idempotency (per-task key)
- drain happy path: claim → FakeVision → task waiting_confirm, job succeeded
- the QUEUED/PROCESSING guard lets a crashed-mid-PROCESSING task reprocess on retry
- invalid payload surfaces as a structured failure, not a crash
- dead-letter reconcile flips a stuck queued/processing task to 'failed'
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.common.enums import ImportStatus
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.import_task import ImportTask
from app.infrastructure.persistence.models.user import User
from app.infrastructure.workers import import_worker
from app.infrastructure.workers.import_worker import (
    IMPORT_JOB_TYPE,
    drain_import_jobs,
    enqueue_import_task,
    reconcile_dead_import_tasks,
)
from app.infrastructure.workers.job_runtime import (
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    claim_one,
    enqueue,
)


@pytest_asyncio.fixture
async def patched_session_factory(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(postgres_url, future=True)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(import_worker, "AsyncSessionLocal", factory)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_task(
    factory: async_sessionmaker[AsyncSession],
    *,
    task_status: str = ImportStatus.QUEUED.value,
    input_type: str = "image",
) -> tuple[uuid.UUID, uuid.UUID]:
    user_id = uuid.uuid4()
    async with factory() as s:
        s.add(User(id=user_id))
        await s.flush()
        task = ImportTask(
            user_id=user_id,
            input_type=input_type,
            source_type="album",
            task_status=task_status,
            input_payload={"image_url": "https://cdn/shirt.jpg"},
        )
        s.add(task)
        await s.flush()
        task_id = task.id
        await s.commit()
    return user_id, task_id


async def test_enqueue_idempotent_per_task(
    patched_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id, task_id = await _seed_task(patched_session_factory)
    async with patched_session_factory() as s:
        first, ins_first = await enqueue_import_task(s, task_id=task_id, user_id=user_id)
        second, ins_second = await enqueue_import_task(s, task_id=task_id, user_id=user_id)
        await s.commit()
    assert ins_first is True
    assert ins_second is False
    assert first.id == second.id
    assert first.idempotency_key == f"import_task:{task_id}"
    assert first.target_id == task_id


async def test_drain_processes_task_to_waiting_confirm(
    patched_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id, task_id = await _seed_task(patched_session_factory)
    async with patched_session_factory() as s:
        job, _ = await enqueue_import_task(s, task_id=task_id, user_id=user_id)
        await s.commit()
        job_id = job.id

    processed = await drain_import_jobs(ctx=None)
    assert processed == 1

    async with patched_session_factory() as s:
        finalized = await s.get(AsyncJob, job_id)
        assert finalized is not None
        # Business-success: the job executed (FakeVision), so it's succeeded
        # and the task advanced to its business waiting_confirm state.
        assert finalized.status == JOB_STATUS_SUCCEEDED
        task = await s.get(ImportTask, task_id)
        assert task is not None
        assert task.task_status == ImportStatus.WAITING_CONFIRM.value
        assert task.candidate_payload is not None


async def test_drain_reprocesses_task_stuck_in_processing(
    patched_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    # A prior async-job attempt crashed mid-flight, leaving the task in
    # PROCESSING. The QUEUED/PROCESSING guard must let the retry reprocess it
    # rather than no-op-succeed and strand the task forever.
    user_id, task_id = await _seed_task(
        patched_session_factory, task_status=ImportStatus.PROCESSING.value
    )
    async with patched_session_factory() as s:
        job, _ = await enqueue_import_task(s, task_id=task_id, user_id=user_id)
        await s.commit()

    assert await drain_import_jobs(ctx=None) == 1

    async with patched_session_factory() as s:
        task = await s.get(ImportTask, task_id)
        assert task is not None
        assert task.task_status == ImportStatus.WAITING_CONFIRM.value


async def test_drain_recovers_expired_import_without_recovering_other_queues(
    patched_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id, task_id = await _seed_task(patched_session_factory)
    lease_expired = datetime.now(UTC) - timedelta(minutes=2)
    async with patched_session_factory() as session:
        import_job, _ = await enqueue_import_task(session, task_id=task_id, user_id=user_id)
        other_job, _ = await enqueue(session, job_type="embedding", idempotency_key=f"test:{task_id}")
        await session.commit()
        import_job_id, other_job_id = import_job.id, other_job.id
        for job in (import_job, other_job):
            job.next_run_at = lease_expired
        await session.commit()
        assert await claim_one(session, [IMPORT_JOB_TYPE], now=lease_expired, lease_seconds=1)
        assert await claim_one(session, ["embedding"], now=lease_expired, lease_seconds=1)

    assert await drain_import_jobs(ctx=None) == 1
    assert await drain_import_jobs(ctx=None) == 0

    async with patched_session_factory() as session:
        recovered = await session.get(AsyncJob, import_job_id)
        untouched = await session.get(AsyncJob, other_job_id)
        task = await session.get(ImportTask, task_id)
        assert recovered is not None and recovered.status == JOB_STATUS_SUCCEEDED
        assert recovered.attempts == 2
        assert untouched is not None and untouched.status == JOB_STATUS_RUNNING
        assert task is not None and task.task_status == ImportStatus.WAITING_CONFIRM.value


async def test_drain_skips_invalid_payload(
    patched_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with patched_session_factory() as s:
        job, _ = await enqueue(
            s,
            job_type=IMPORT_JOB_TYPE,
            idempotency_key=f"import_task:bad:{uuid.uuid4()}",
            payload={},  # missing task_id / user_id
        )
        await s.commit()
        job_id = job.id

    assert await drain_import_jobs(ctx=None) == 1

    async with patched_session_factory() as s:
        final = await s.get(AsyncJob, job_id)
        assert final is not None
        assert final.status == JOB_STATUS_QUEUED  # retried with backoff
        assert final.last_error == "missing payload fields"


async def test_reconcile_dead_marks_task_failed(
    patched_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id, task_id = await _seed_task(
        patched_session_factory, task_status=ImportStatus.PROCESSING.value
    )
    async with patched_session_factory() as s:
        s.add(
            AsyncJob(
                job_type=IMPORT_JOB_TYPE,
                idempotency_key=f"import_task:{task_id}",
                status="dead",
                target_id=task_id,
                user_id=user_id,
                payload={"task_id": str(task_id), "user_id": str(user_id)},
            )
        )
        await s.commit()

    assert await reconcile_dead_import_tasks(ctx=None) == 1

    async with patched_session_factory() as s:
        task = await s.get(ImportTask, task_id)
        assert task is not None
        assert task.task_status == ImportStatus.FAILED.value
        assert task.error_code == "import_failed"


def test_drain_constants_are_sane() -> None:
    assert 1 <= import_worker.IMPORT_MAX_DRAIN_PER_TICK <= 50
    assert import_worker.IMPORT_JOB_TYPE == "import_task"


@pytest.mark.parametrize("approved", [[True, True], [True, False]])
async def test_outfit_photo_stores_only_approved_crops(
    patched_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch, approved: list[bool]
) -> None:
    import io

    from PIL import Image

    from app.infrastructure.workers import garment_crops

    photo = io.BytesIO()
    Image.new("RGB", (300, 600), (200, 30, 30)).save(photo, "JPEG")
    stored: dict[str, bytes] = {}

    class _Storage:
        async def put_object(self, key: str, data: bytes, content_type: str) -> str:
            stored[key] = data
            return f"https://fake-storage/{key}"

    async def _load(source_url: str, source_key: str | None) -> bytes:
        return photo.getvalue()

    async def _review(self: object, crops: list[bytes], garments: list[object]) -> list[bool]:
        assert len(crops) == len(garments) == 2
        return approved

    monkeypatch.setattr("app.bootstrap.container.object_storage", lambda: _Storage())
    monkeypatch.setattr(garment_crops, "load_source_bytes", _load)
    monkeypatch.setattr("app.infrastructure.vision.fake_vision.FakeVision.review_garment_crops", _review)

    user_id, task_id = await _seed_task(patched_session_factory, input_type="outfit_image")
    async with patched_session_factory() as s:
        await enqueue_import_task(s, task_id=task_id, user_id=user_id)
        await s.commit()
    assert await drain_import_jobs(ctx=None) == 1

    async with patched_session_factory() as s:
        task = await s.get(ImportTask, task_id)
        assert task is not None and task.candidate_payload is not None
        candidates = task.candidate_payload["candidate_items"]
    assert len(candidates) == 2
    for candidate, passed in zip(candidates, approved, strict=True):
        assert candidate["original_image_url"] == "https://cdn/shirt.jpg"
        assert len(candidate["bbox"]) == 4
        if not passed:
            assert "crop_image_url" not in candidate and "crop_object_key" not in candidate
            continue
        key = f"users/{user_id}/wardrobe/verified-crops/{task_id}/{candidate['temp_item_id']}.jpg"
        assert candidate["crop_object_key"] == key
        assert candidate["crop_image_url"] == f"https://fake-storage/{key}"
    assert len(stored) == sum(approved)


async def test_crop_failure_keeps_the_full_photo(
    patched_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch
) -> None:
    from app.infrastructure.workers import garment_crops

    async def _broken(source_url: str, source_key: str | None) -> bytes:
        raise OSError("storage down")

    monkeypatch.setattr(garment_crops, "load_source_bytes", _broken)
    user_id, task_id = await _seed_task(patched_session_factory, input_type="outfit_image")
    async with patched_session_factory() as s:
        await enqueue_import_task(s, task_id=task_id, user_id=user_id)
        await s.commit()
    assert await drain_import_jobs(ctx=None) == 1

    async with patched_session_factory() as s:
        task = await s.get(ImportTask, task_id)
        assert task is not None and task.task_status == ImportStatus.WAITING_CONFIRM.value
        assert task.candidate_payload is not None
        assert all("crop_image_url" not in c for c in task.candidate_payload["candidate_items"])
