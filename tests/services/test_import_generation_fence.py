"""Exercise both stale recognition finalizers after a manual import retry."""

import asyncio
import io
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from fastapi import FastAPI
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.ports.vision import VisionResult
from app.common.enums import ImportStatus
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.import_task import ImportTask
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.session import get_db_session
from app.infrastructure.workers import import_worker
from app.infrastructure.workers.job_runtime import claim_one, recover_expired_leases
from app.presentation.api.v1.routers.import_tasks import router


@pytest_asyncio.fixture
async def worker_sessions(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(postgres_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(import_worker, "AsyncSessionLocal", factory)
    try:
        yield factory
    finally:
        await engine.dispose()


@pytest.mark.parametrize("late_error", [False, True], ids=["old-success", "old-failure"])
@pytest.mark.parametrize("new_checkpoint", [False, True], ids=["new-processing", "new-checkpointed"])
async def test_old_import_finalizer_leaves_retried_generation_untouched(
    worker_sessions: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    late_error: bool,
    new_checkpoint: bool,
) -> None:
    user_id = uuid.uuid4()
    image_key = f"users/{user_id}/wardrobe/shirt.jpg"
    image_url = f"http://localhost:9000/local/files/{image_key}"
    image = io.BytesIO()
    Image.new("RGB", (32, 32), (8, 32, 60)).save(image, "JPEG")
    old_started, release_old = asyncio.Event(), asyncio.Event()
    new_started, release_new = asyncio.Event(), asyncio.Event()
    source_reads: list[str] = []
    calls: list[int] = []

    async def read_source(url: str, key: str, *, user_id: uuid.UUID) -> bytes:
        assert url == image_url and key == image_key
        assert key.startswith(f"users/{user_id}/")
        source_reads.append(key)
        return image.getvalue()

    class BlockingVision:
        async def analyze_image(self, data_uri: str, locale: str) -> VisionResult:
            assert data_uri.startswith("data:image/jpeg;base64,")
            attempt = len(calls)
            calls.append(attempt)
            if attempt == 0:
                old_started.set()
                await release_old.wait()
                if late_error:
                    request = httpx.Request("POST", "https://vision.invalid")
                    response = httpx.Response(401, request=request)
                    raise httpx.HTTPStatusError("late provider failure", request=request, response=response)
                return VisionResult(title="Old shirt", category="top", color_main="red", confidence_score=0.9)
            assert attempt == 1
            new_started.set()
            await release_new.wait()
            return VisionResult(title="New shirt", category="top", color_main="blue", confidence_score=0.95)

    monkeypatch.setattr(import_worker, "load_source_bytes", read_source)
    monkeypatch.setattr(import_worker, "vision_service", BlockingVision)
    async with worker_sessions() as session:
        session.add(User(id=user_id))
        await session.flush()
        task = ImportTask(
            user_id=user_id,
            input_type="image",
            source_type="album",
            input_payload={"image_url": image_url, "source_object_key": image_key},
        )
        session.add(task)
        await session.flush()
        task_id = task.id
        old, _ = await import_worker.enqueue_import_task(session, task_id=task_id, user_id=user_id)
        old.max_attempts = 1
        await session.commit()
        old_claim = await claim_one(session, [import_worker.IMPORT_JOB_TYPE])
        assert old_claim is not None and old_claim.id == old.id and old_claim.attempts == 1

    app = FastAPI()

    async def request_session() -> AsyncIterator[AsyncSession]:
        async with worker_sessions() as session:
            yield session
            await session.commit()

    app.dependency_overrides[get_db_session] = request_session
    app.include_router(router)

    old_runner = asyncio.create_task(import_worker._process_one_import(old_claim))
    new_runner: asyncio.Task[None] | None = None
    try:
        await asyncio.wait_for(old_started.wait(), timeout=5)
        async with worker_sessions() as session:
            old_task = await session.get(ImportTask, task_id)
            assert old_task is not None
            assert old_task.run_generation == 0 and old_task.task_status == ImportStatus.PROCESSING
            assert old_task.candidate_payload is None
            assert await recover_expired_leases(
                session, now=datetime.now(UTC) + timedelta(hours=1), job_type=import_worker.IMPORT_JOB_TYPE,
            ) == 1
            await session.commit()
        assert await import_worker.reconcile_dead_import_tasks(ctx=None) == 1

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
            response = await client.post(f"/v1/import-tasks/{task_id}/retry", headers={"X-User-Id": str(user_id)})
        assert response.status_code == 200
        assert response.json()["data"]["task_id"] == str(task_id)
        assert response.json()["data"]["status"] == "queued"

        async with worker_sessions() as session:
            new_claim = await claim_one(session, [import_worker.IMPORT_JOB_TYPE])
            assert new_claim is not None and new_claim.id != old_claim.id and new_claim.attempts == 1
            assert new_claim.payload is not None and new_claim.payload["generation"] == 1
        new_runner = asyncio.create_task(import_worker._process_one_import(new_claim))
        await asyncio.wait_for(new_started.wait(), timeout=5)
        if new_checkpoint:
            release_new.set()
            await asyncio.wait_for(new_runner, timeout=5)

        async with worker_sessions() as session:
            before = await session.get(ImportTask, task_id)
            assert before is not None and before.run_generation == 1
            assert before.task_status == (ImportStatus.WAITING_CONFIRM if new_checkpoint else ImportStatus.PROCESSING)
            prior_candidates = before.candidate_payload
            if new_checkpoint:
                assert prior_candidates is not None and prior_candidates["title"] == "New shirt"
            else:
                assert prior_candidates is None
            prior_input = before.input_payload
            prior_status = before.task_status

        release_old.set()
        await asyncio.wait_for(old_runner, timeout=5)
        async with worker_sessions() as session:
            after = await session.get(ImportTask, task_id)
            assert after is not None and after.run_generation == 1
            assert after.task_status == prior_status
            assert after.candidate_payload == prior_candidates
            assert after.input_payload == prior_input
            assert after.error_code is None and after.error_message is None and after.retryable is None
            old_job = await session.get(AsyncJob, old_claim.id)
            assert old_job is not None and old_job.status == "dead" and old_job.attempts == 1
            assert old_job.last_error == "lease_expired" and old_job.result is None
            new_job = await session.get(AsyncJob, new_claim.id)
            assert new_job is not None and new_job.status == ("succeeded" if new_checkpoint else "running")
        release_new.set()
        await asyncio.wait_for(new_runner, timeout=5)
    finally:
        release_old.set()
        release_new.set()
        runners = [old_runner] if new_runner is None else [old_runner, new_runner]
        await asyncio.gather(*runners, return_exceptions=True)

    async with worker_sessions() as session:
        final = await session.get(ImportTask, task_id)
        assert final is not None and final.run_generation == 1 and final.task_status == ImportStatus.WAITING_CONFIRM
        assert final.candidate_payload is not None
        assert [candidate["title"] for candidate in final.candidate_payload["candidate_items"]] == ["New shirt"]
        jobs = list(await session.scalars(select(AsyncJob).where(AsyncJob.target_id == task_id)))
        assert {(job.status, job.attempts) for job in jobs} == {("dead", 1), ("succeeded", 1)}
        assert len(jobs) == 2
    assert source_reads == [image_key, image_key] and calls == [0, 1]
