"""Import worker on job_runtime (BE#26).

Cover:
- enqueue idempotency (per-task key)
- drain happy path: claim → FakeVision → task waiting_confirm, job succeeded
- the QUEUED/PROCESSING guard lets a crashed-mid-PROCESSING task reprocess on retry
- invalid payload surfaces as a structured failure, not a crash
- dead-letter reconcile flips a stuck queued/processing task to 'failed'
"""

from __future__ import annotations

import io
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import httpx
import pytest
import pytest_asyncio
from PIL import Image
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.ports.vision import VisionResult
from app.common.enums import ImportStatus
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.import_task import ImportTask
from app.infrastructure.persistence.models.user import User
from app.infrastructure.vision.fake_vision import FakeVision
from app.infrastructure.workers import garment_crops, import_worker
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
    objects: dict[str, bytes] = {}
    image = io.BytesIO()
    Image.new("RGB", (300, 600), (200, 30, 30)).save(image, "JPEG")

    async def download(url: str, settings: object) -> tuple[bytes, str]:
        return image.getvalue(), "image/jpeg"

    class Store:
        async def put_object(self, key: str, data: bytes, content_type: str) -> str:
            objects[key] = data
            return f"http://localhost:9000/local/files/{key}"

        async def get_object(self, key: str) -> bytes:
            return objects[key]

        def sign_render_url(self, key: str) -> str:
            assert key in objects
            return f"http://localhost:9000/local/files/{key}?signed=fixture"

    monkeypatch.setattr(import_worker, "AsyncSessionLocal", factory)
    monkeypatch.setattr(garment_crops, "AsyncSessionLocal", factory)
    monkeypatch.setattr(import_worker, "download_external_source_image", download)
    monkeypatch.setattr(import_worker, "object_storage", Store)
    monkeypatch.setattr("app.bootstrap.container.object_storage", Store)
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


async def test_crop_retry_preserves_recognized_candidates(
    patched_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_load = garment_crops.load_source_bytes
    calls = 0

    async def load(url: str | None, key: str | None, **kwargs) -> bytes:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("transient storage outage")
        return await real_load(url, key, **kwargs)

    monkeypatch.setattr(garment_crops, "load_source_bytes", load)
    user_id, task_id = await _seed_task(patched_session_factory, input_type="outfit_image")
    async with patched_session_factory() as session:
        await enqueue_import_task(session, task_id=task_id, user_id=user_id)
        await session.commit()
    assert await drain_import_jobs(ctx=None) == 1
    assert await garment_crops.drain_garment_crop_jobs(ctx=None) == 2

    async with patched_session_factory() as session:
        task = await session.get(ImportTask, task_id)
        assert task is not None and task.task_status == ImportStatus.WAITING_CONFIRM
        initial = task.candidate_payload["candidate_items"]
        assert len(initial) == 2
        assert sum(bool(c.get("crop_object_key")) for c in initial) == 1
        queued = await session.scalar(
            select(AsyncJob).where(AsyncJob.job_type == garment_crops.CROP_JOB_TYPE, AsyncJob.status == "queued"),
        )
        assert queued is not None and queued.attempts == 1
        queued.next_run_at = datetime.now(UTC)
        await session.commit()

    assert await drain_import_jobs(ctx=None) == 0
    assert await garment_crops.drain_garment_crop_jobs(ctx=None) == 1
    async with patched_session_factory() as session:
        task = await session.get(ImportTask, task_id)
        final = task.candidate_payload["candidate_items"]
        assert [c["temp_item_id"] for c in final] == [c["temp_item_id"] for c in initial]
        assert len({c["crop_object_key"] for c in final}) == 2
        prefix = f"users/{user_id}/wardrobe/verified-crops/{task_id}/"
        assert all(c["crop_object_key"].startswith(prefix) for c in final)
        assert all(c["original_image_url"] == task.input_payload["image_url"] for c in final)


async def test_abandoned_crop_lease_recovers_without_repeating_recognition(
    patched_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch,
) -> None:
    vision_calls = 0

    class Vision(FakeVision):
        async def analyze_outfit(self, image_url: str, locale: str = "zh-CN") -> list[VisionResult]:
            nonlocal vision_calls
            vision_calls += 1
            return await super().analyze_outfit(image_url, locale)

    monkeypatch.setattr(import_worker, "vision_service", Vision)
    user_id, task_id = await _seed_task(patched_session_factory, input_type="outfit_image")
    async with patched_session_factory() as session:
        recognition, _ = await enqueue_import_task(session, task_id=task_id, user_id=user_id)
        recognition_id = recognition.id
        await session.commit()

    assert await drain_import_jobs(ctx=None) == 1
    async with patched_session_factory() as session:
        task = await session.get(ImportTask, task_id)
        assert task is not None and task.task_status == ImportStatus.WAITING_CONFIRM
        assert task.candidate_payload is not None
        initial = task.candidate_payload["candidate_items"]
        assert len(initial) == 2 and all(not candidate.get("crop_object_key") for candidate in initial)
        source_key = task.input_payload["source_object_key"]
        assert source_key
        queued = list(await session.scalars(
            select(AsyncJob).where(AsyncJob.target_id == task_id, AsyncJob.job_type == garment_crops.CROP_JOB_TYPE)
        ))
        assert len(queued) == 2
        # Persist a real claim that a worker never finalizes. Backdate only
        # the chosen job's eligibility and claim so recovery needs no sleep.
        claim_time = datetime.now(UTC) - timedelta(minutes=1)
        queued[0].next_run_at = claim_time
        await session.commit()
        abandoned = await claim_one(
            session, [garment_crops.CROP_JOB_TYPE], now=claim_time, lease_seconds=1,
        )
        assert abandoned is not None and abandoned.status == JOB_STATUS_RUNNING
        assert abandoned.attempts == 1
        abandoned_id = abandoned.id

    assert await garment_crops.drain_garment_crop_jobs(ctx=None) == 2
    assert await drain_import_jobs(ctx=None) == 0
    assert vision_calls == 1
    async with patched_session_factory() as session:
        task = await session.get(ImportTask, task_id)
        assert task is not None and task.task_status == ImportStatus.WAITING_CONFIRM
        assert task.input_payload["source_object_key"] == source_key
        assert task.candidate_payload is not None
        final = task.candidate_payload["candidate_items"]
        assert [
            {key: value for key, value in candidate.items() if key not in {"crop_object_key", "crop_image_url"}}
            for candidate in final
        ] == initial
        assert len({candidate["crop_object_key"] for candidate in final}) == 2
        recovered = await session.get(AsyncJob, abandoned_id)
        assert recovered is not None and recovered.status == JOB_STATUS_SUCCEEDED and recovered.attempts == 2
        recovered_candidate = next(
            candidate for candidate in final if candidate["temp_item_id"] == recovered.payload["candidate_id"]
        )
        assert recovered_candidate["crop_object_key"].endswith(f"/{abandoned_id}/2.jpg")
        recognition = await session.get(AsyncJob, recognition_id)
        assert recognition is not None and recognition.status == JOB_STATUS_SUCCEEDED and recognition.attempts == 1


@pytest.mark.parametrize("status", [429, 503, "network"])
async def test_provider_error_retries_using_committed_snapshot(
    patched_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch, status: int | str,
) -> None:
    vision_calls = 0
    download_calls = 0
    download_image = import_worker.download_external_source_image

    class Vision(FakeVision):
        async def analyze_image(self, image_url: str, locale: str = "zh-CN"):
            nonlocal vision_calls
            vision_calls += 1
            assert image_url.startswith("data:image/jpeg;base64,")
            if vision_calls == 1:
                request = httpx.Request("POST", "https://provider.test/recognize?token=secret")
                if status == "network":
                    raise httpx.ConnectError("network unavailable", request=request)
                response = httpx.Response(status, request=request)
                response.raise_for_status()
            return await super().analyze_image(image_url, locale)

    async def download(url: str, settings: object):
        nonlocal download_calls
        download_calls += 1
        assert download_calls == 1, "source is no longer available"
        return await download_image(url, settings)

    monkeypatch.setattr(import_worker, "download_external_source_image", download)
    monkeypatch.setattr(import_worker, "vision_service", Vision)
    user_id, task_id = await _seed_task(patched_session_factory)
    async with patched_session_factory() as session:
        job, _ = await enqueue_import_task(session, task_id=task_id, user_id=user_id)
        job_id = job.id
        await session.commit()

    assert await drain_import_jobs(ctx=None) == 1
    async with patched_session_factory() as session:
        task = await session.get(ImportTask, task_id)
        job = await session.get(AsyncJob, job_id)
        assert task is not None and task.task_status == ImportStatus.PROCESSING
        assert task.candidate_payload is None
        assert task.input_payload["source_object_key"]
        assert job is not None and job.last_error == "provider_unavailable"
        assert job.status == JOB_STATUS_QUEUED and job.attempts == 1
        assert job.next_run_at > job.updated_at
        early_claim = await claim_one(
            session, [IMPORT_JOB_TYPE], now=job.next_run_at - timedelta(microseconds=1),
        )
        assert early_claim is None
        job.next_run_at = datetime.now(UTC)
        await session.commit()

    assert await drain_import_jobs(ctx=None) == 1
    async with patched_session_factory() as session:
        task = await session.get(ImportTask, task_id)
        assert task is not None and task.task_status == ImportStatus.WAITING_CONFIRM
        assert len(task.candidate_payload["candidate_items"]) == 1
        job = await session.get(AsyncJob, job_id)
        assert job is not None and job.status == JOB_STATUS_SUCCEEDED and job.attempts == 2
    assert vision_calls == 2
    assert download_calls == 1


@pytest.mark.parametrize("mode", ["empty", "invalid", "bad_auth"])
async def test_recognition_rejection_preserves_a_sanitized_business_outcome(
    patched_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch, mode: str,
) -> None:
    class Vision(FakeVision):
        async def analyze_outfit(self, image_url: str, locale: str = "zh-CN"):
            if mode == "empty":
                return []
            response = httpx.Response(
                403, request=httpx.Request("POST", "https://example.com/model?token=discard"),
            )
            response.raise_for_status()

    if mode == "invalid":
        def reject_image(data: bytes) -> str:
            raise import_worker.InvalidSourceImage("private content")

        monkeypatch.setattr(import_worker, "image_data_uri", reject_image)
    monkeypatch.setattr(import_worker, "vision_service", Vision)
    user_id, task_id = await _seed_task(patched_session_factory, input_type="outfit_image")
    async with patched_session_factory() as session:
        job, _ = await enqueue_import_task(session, task_id=task_id, user_id=user_id)
        job_id = job.id
        await session.commit()

    assert await drain_import_jobs(ctx=None) == 1
    async with patched_session_factory() as session:
        task = await session.get(ImportTask, task_id)
        job = await session.get(AsyncJob, job_id)
        assert task.task_status == ImportStatus.FAILED and task.retryable is False
        expected = {"empty": "no_garments", "invalid": "image_invalid", "bad_auth": "provider_configuration"}
        assert task.error_code == expected[mode]
        assert task.candidate_payload is None
        assert job.status == JOB_STATUS_SUCCEEDED
        assert job.result["outcome"] == "rejected"
        assert "private content" not in task.error_message
        assert "token=" not in task.error_message


async def test_import_cancellation_releases_claim_without_resetting_attempts(
    patched_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    entered = asyncio.Event()

    async def wait_for_cancel(*args: object) -> None:
        entered.set()
        await asyncio.Future()

    monkeypatch.setattr(import_worker, "_recognize", wait_for_cancel)
    user_id, task_id = await _seed_task(patched_session_factory)
    async with patched_session_factory() as session:
        job, _ = await enqueue_import_task(session, task_id=task_id, user_id=user_id)
        job_id = job.id
        await session.commit()

    drainer = asyncio.create_task(drain_import_jobs(ctx=None))
    await asyncio.wait_for(entered.wait(), timeout=10)
    drainer.cancel()
    with pytest.raises(asyncio.CancelledError):
        await drainer
    async with patched_session_factory() as session:
        job = await session.get(AsyncJob, job_id)
        assert job.status == JOB_STATUS_QUEUED and job.attempts == 1
        assert job.next_run_at > datetime.now(UTC)
        assert job.last_error == "attempt_cancelled"


async def test_parallel_retry_creates_only_one_generation_and_fences_old_jobs(
    patched_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    import asyncio

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.infrastructure.persistence.session import get_db_session
    from app.presentation.api.v1.routers.import_tasks import router

    user_id, task_id = await _seed_task(patched_session_factory, task_status=ImportStatus.FAILED)
    async with patched_session_factory() as session:
        task = await session.get(ImportTask, task_id)
        task.retryable = True
        dead, _ = await enqueue_import_task(session, task_id=task_id, user_id=user_id)
        dead.status, dead.attempts = "dead", 3
        await session.commit()

    app = FastAPI()

    async def request_session() -> AsyncIterator[AsyncSession]:
        async with patched_session_factory() as session:
            yield session
            await session.commit()

    app.dependency_overrides[get_db_session] = request_session
    app.include_router(router)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        path = f"/v1/import-tasks/{task_id}/retry"
        headers = {"X-User-Id": str(user_id)}
        responses = await asyncio.gather(client.post(path, headers=headers), client.post(path, headers=headers))
    assert [response.status_code for response in responses] == [200, 200]
    assert {response.json()["data"]["task_id"] for response in responses} == {str(task_id)}
    assert await reconcile_dead_import_tasks(ctx=None) == 0

    async with patched_session_factory() as session:
        task = await session.get(ImportTask, task_id)
        assert task.run_generation == 1 and task.task_status == ImportStatus.QUEUED
        jobs = list(await session.scalars(select(AsyncJob).where(AsyncJob.target_id == task_id)))
        assert len(jobs) == 2
        assert {(job.status, job.attempts) for job in jobs} == {("queued", 0), ("dead", 3)}
        assert [job.payload["generation"] for job in jobs if job.status == "queued"] == [1]


async def test_import_crop_first_outfit_pipeline(
    patched_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch, tmp_path,
) -> None:
    import json
    import os
    from dataclasses import replace
    from pathlib import Path

    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from app.application.import_tasks import prepare_derivatives
    from app.config.settings import Settings
    from app.infrastructure.persistence.models.paid_generation import PaidGenerationReservation
    from app.infrastructure.persistence.models.wardrobe import WardrobeItemImage
    from app.infrastructure.persistence.session import get_db_session
    from app.presentation.api.v1.routers import import_tasks, recommendations

    class Vision(FakeVision):
        calls = 0

        async def analyze_outfit(self, image_url: str, locale: str = "zh-CN"):
            self.calls += 1
            top, bottom = await super().analyze_outfit(image_url, locale)
            return [
                top, bottom,
                replace(
                    bottom, title="Black shoes", category="shoes", canonical_name="Black shoes",
                    display_name="Black shoes", bbox=(0.3, 0.80, 0.70, 0.99),
                ),
            ]

    vision = Vision()
    monkeypatch.setattr(import_worker, "vision_service", lambda: vision)
    monkeypatch.setattr(import_tasks, "get_settings", lambda: Settings(ENABLE_IMPORT_ASYNC_JOBS=True))
    monkeypatch.setattr(prepare_derivatives, "get_settings", lambda: Settings(ENABLE_ITEM_CUTOUT=True))

    app = FastAPI()

    async def request_session() -> AsyncIterator[AsyncSession]:
        async with patched_session_factory() as session:
            yield session
            await session.commit()

    app.dependency_overrides[get_db_session] = request_session
    app.include_router(import_tasks.router)
    app.include_router(recommendations.router)
    user_id = uuid.uuid4()
    headers = {"X-User-Id": str(user_id)}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            "/v1/import-tasks", headers=headers,
            json={"input_type": "outfit_image", "source_type": "album", "image_url": "https://shop.test/look.jpg"},
        )
        assert response.status_code == 202
        task_id = response.json()["data"]["task_id"]
        path = f"/v1/import-tasks/{task_id}"
        assert await drain_import_jobs(ctx=None) == 1
        candidates = (await client.get(path, headers=headers)).json()["data"]["candidate_items"]
        assert len(candidates) == 3 and all(candidate["crop_image_url"] is None for candidate in candidates)

        def entry(candidate: dict[str, str]) -> dict[str, str]:
            return {key: candidate[key] for key in ("temp_item_id", "title", "category", "color_main")}

        first = await client.post(f"{path}/confirm", headers=headers, json=entry(candidates[0]))
        first_item = first.json()["data"]["item_id"]
        replay = await client.post(
            f"{path}/confirm-items", headers=headers, json={"items": [entry(candidates[0])]},
        )
        assert replay.json()["data"]["task_status"] == "waiting_confirm"
        assert replay.json()["data"]["created_items"][0]["item_id"] == first_item
        result = await client.get("/v1/recommendations/first-outfit", headers=headers)
        assert result.json()["data"]["state"] == "no_outfit"
        async with patched_session_factory() as session:
            queued = await session.scalar(
                select(AsyncJob).where(AsyncJob.target_id == uuid.UUID(first_item), AsyncJob.job_type == "cutout")
            )
            assert queued is None

        load = garment_crops.load_source_bytes
        crop_calls = 0

        async def fail_first_crop(url: str | None, key: str | None, **kwargs) -> bytes:
            nonlocal crop_calls
            crop_calls += 1
            if crop_calls == 1:
                raise OSError("temporary storage outage")
            return await load(url, key, **kwargs)

        monkeypatch.setattr(garment_crops, "load_source_bytes", fail_first_crop)
        assert await garment_crops.drain_garment_crop_jobs(ctx=None) == 3
        async with patched_session_factory() as session:
            waiting_crop = await session.scalar(
                select(AsyncJob).where(AsyncJob.job_type == garment_crops.CROP_JOB_TYPE, AsyncJob.status == "queued"),
            )
            assert waiting_crop is not None and waiting_crop.attempts == 1
            waiting_crop.next_run_at = datetime.now(UTC)
            await session.commit()

        remaining = await client.post(
            f"{path}/confirm-items", headers=headers,
            json={"items": [entry(candidate) for candidate in candidates[1:]]},
        )
        assert remaining.json()["data"]["task_status"] == "completed"
        assert await garment_crops.drain_garment_crop_jobs(ctx=None) == 1
        assert await drain_import_jobs(ctx=None) == 0

        outfit = (await client.get("/v1/recommendations/first-outfit", headers=headers)).json()["data"]
        final = (await client.get(path, headers=headers)).json()["data"]

    assert outfit["state"] in {"single_outfit", "ready"}
    mapping = {candidate["temp_item_id"]: candidate["confirmed_item_id"] for candidate in final["candidate_items"]}
    assert len(mapping) == len(set(mapping.values())) == 3 and None not in mapping.values()
    assert mapping[candidates[0]["temp_item_id"]] == first_item
    assert all(candidate["crop_image_url"] for candidate in final["candidate_items"])
    outfit_ids = [item["item_id"] for item in outfit["first_outfit"]["items"]]
    assert set(outfit_ids) == set(mapping.values())
    async with patched_session_factory() as session:
        ids = [uuid.UUID(value) for value in mapping.values()]
        images = list(
            await session.scalars(
                select(WardrobeItemImage).where(
                    WardrobeItemImage.item_id.in_(ids), WardrobeItemImage.image_type == "crop",
                )
            )
        )
        assert {str(image.item_id) for image in images} == set(mapping.values())
        assert len({image.object_key for image in images}) == 3
        embeds = list(
            await session.scalars(select(AsyncJob).where(AsyncJob.job_type == "embedding", AsyncJob.user_id == user_id))
        )
        assert len(embeds) == 3
        assert {job.target_id for job in embeds} == set(ids)
        cutout_jobs = list(
            await session.scalars(select(AsyncJob).where(AsyncJob.job_type == "cutout", AsyncJob.user_id == user_id))
        )
        assert len(cutout_jobs) == 3 and {job.target_id for job in cutout_jobs} == set(ids)
        held = list(await session.scalars(
            select(PaidGenerationReservation).where(
                PaidGenerationReservation.user_id == user_id,
                PaidGenerationReservation.job_type == "cutout",
            )
        ))
        assert len(held) == 3 and {reservation.job_id for reservation in held} == {job.id for job in cutout_jobs}
        assert all(reservation.target_id is None for reservation in held)
    assert vision.calls == 1 and crop_calls == 4

    output_dir = Path(os.environ.get("IMPORT_PIPELINE_EVIDENCE_DIR", tmp_path))
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / "A23.json").write_text(
        json.dumps(
            {
                "input": "synthetic 300x600 photo, three boxed garments",
                "recognition_calls": vision.calls,
                "crop_attempts": crop_calls,
                "cutout_reservations": len(held),
                "pending_after_first_confirmation": True,
                "confirmed_mapping": mapping,
                "first_outfit_state": outfit["state"],
                "first_outfit_item_ids": outfit_ids,
                "final_task_status": final["status"],
            },
            indent=2,
        ),
        encoding="utf-8",
    )


async def test_stale_crop_upload_cannot_replace_adopted_image(
    patched_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch,
) -> None:
    import asyncio

    from app.bootstrap import container
    from app.infrastructure.workers.job_runtime import recover_expired_leases

    user_id, task_id = await _seed_task(patched_session_factory, input_type="outfit_image")
    async with patched_session_factory() as session:
        await enqueue_import_task(session, task_id=task_id, user_id=user_id)
        await session.commit()
    assert await drain_import_jobs(ctx=None) == 1
    async with patched_session_factory() as session:
        old = await claim_one(session, [garment_crops.CROP_JOB_TYPE])
        assert old is not None
        payload = old.payload

    uploading = asyncio.Event()
    resume = asyncio.Event()
    store = container.object_storage()
    written: list[str] = []

    class DelayedStore:
        async def get_object(self, key: str) -> bytes:
            return await store.get_object(key)

        async def put_object(self, key: str, data: bytes, content_type: str) -> str:
            if key.endswith("/1.jpg"):
                uploading.set()
                await resume.wait()
            written.append(key)
            return await store.put_object(key, data, content_type)

    monkeypatch.setattr(container, "object_storage", DelayedStore)
    stale = asyncio.create_task(garment_crops._process_crop(old))
    try:
        await asyncio.wait_for(uploading.wait(), timeout=10)
        async with patched_session_factory() as session:
            assert await recover_expired_leases(
                session, now=datetime.now(UTC) + timedelta(minutes=11), job_type=garment_crops.CROP_JOB_TYPE,
            ) == 1
            current = await session.get(AsyncJob, old.id)
            current.next_run_at = datetime.now(UTC) - timedelta(seconds=1)
            await session.commit()
            new = await claim_one(session, [garment_crops.CROP_JOB_TYPE])
            assert new is not None and new.id == old.id and new.attempts == 2
        await garment_crops._process_crop(new)
    finally:
        resume.set()
        await stale

    async with patched_session_factory() as session:
        task = await session.get(ImportTask, task_id)
        candidate = next(
            c for c in task.candidate_payload["candidate_items"] if c["temp_item_id"] == payload["candidate_id"]
        )
        assert candidate["crop_object_key"] == written[0]
        assert written[0].endswith("/2.jpg") and written[1].endswith("/1.jpg")
        job = await session.get(AsyncJob, old.id)
        assert job.status == JOB_STATUS_SUCCEEDED and job.attempts == 2



async def test_rejected_crop_does_not_get_adopted_or_repeat_recognition(
    patched_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def review(self, crops, garments):
        assert len(crops) == len(garments) == 1
        return [garments[0].category == "top"]

    monkeypatch.setattr(FakeVision, "review_garment_crops", review)
    user_id, task_id = await _seed_task(patched_session_factory, input_type="outfit_image")
    async with patched_session_factory() as session:
        await enqueue_import_task(session, task_id=task_id, user_id=user_id)
        await session.commit()
    assert await drain_import_jobs(ctx=None) == 1
    assert await garment_crops.drain_garment_crop_jobs(ctx=None) == 2
    assert await drain_import_jobs(ctx=None) == 0

    async with patched_session_factory() as session:
        task = await session.get(ImportTask, task_id)
        assert task.task_status == ImportStatus.WAITING_CONFIRM
        top, bottom = task.candidate_payload["candidate_items"]
        assert top["crop_object_key"].startswith(f"users/{user_id}/wardrobe/verified-crops/")
        assert not bottom.get("crop_object_key")
        results = await session.scalars(
            select(AsyncJob.result).where(AsyncJob.target_id == task_id, AsyncJob.job_type == "garment_crop")
        )
        assert {result["outcome"] for result in results} == {"cropped", "crop_rejected"}
