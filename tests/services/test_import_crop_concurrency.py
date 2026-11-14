"""Confirm and adopt verified crops through independent database transactions."""

from __future__ import annotations

import asyncio
import io
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass

import pytest
import pytest_asyncio
from PIL import Image
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.import_tasks import prepare_derivatives, source
from app.application.import_tasks.candidates import confirmed_item_ids, normalize_candidates, store_candidates
from app.application.import_tasks.confirm_import import ConfirmedEntry, ConfirmImageImportTask, ConfirmItemInput
from app.application.wardrobe.delete_item import DeleteWardrobeItem
from app.bootstrap import container
from app.common.enums import CutoutStatus, ImageType, ImportStatus
from app.config.settings import Settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.import_task import ImportTask
from app.infrastructure.persistence.models.paid_generation import PaidGenerationReservation
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem, WardrobeItemImage
from app.infrastructure.persistence.repositories.import_task_repo import SqlAlchemyImportTaskRepository
from app.infrastructure.persistence.repositories.recommendation_repo import SqlAlchemyRecommendationRepository
from app.infrastructure.persistence.repositories.wardrobe_repo import SqlAlchemyWardrobeRepository
from app.infrastructure.vision.fake_vision import FakeVision
from app.infrastructure.workers import garment_crops
from app.infrastructure.workers.job_runtime import JOB_STATUS_QUEUED, JOB_STATUS_SUCCEEDED, claim_one

_BASE_URL = "http://localhost:9000/local"


@dataclass
class CropStore:
    objects: dict[str, bytes]
    uploading: asyncio.Event | None = None
    resume_upload: asyncio.Event | None = None

    async def get_object(self, key: str) -> bytes:
        return self.objects[key]

    async def put_object(self, key: str, data: bytes, content_type: str) -> str:
        assert content_type == "image/jpeg"
        if self.uploading is not None and self.resume_upload is not None:
            self.uploading.set()
            await self.resume_upload.wait()
        self.objects[key] = data
        return f"{_BASE_URL}/files/{key}"


@pytest_asyncio.fixture
async def crop_environment(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[async_sessionmaker[AsyncSession], CropStore]]:
    engine = create_async_engine(postgres_url)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    settings = Settings(ENABLE_ITEM_CUTOUT=True)
    store = CropStore({})
    monkeypatch.setattr(prepare_derivatives, "get_settings", lambda: settings)
    monkeypatch.setattr(container, "object_storage", lambda: store)
    monkeypatch.setattr(container, "vision_service", FakeVision)
    monkeypatch.setattr(garment_crops, "AsyncSessionLocal", factory)
    try:
        yield factory, store
    finally:
        await engine.dispose()


@dataclass(frozen=True)
class RecognizedImport:
    user_id: uuid.UUID
    task_id: uuid.UUID
    original_key: str
    original_url: str
    entries: list[ConfirmItemInput]


async def _recognized_import(
    factory: async_sessionmaker[AsyncSession], store: CropStore, *, include_bottom: bool,
) -> RecognizedImport:
    user_id, task_id = uuid.uuid4(), uuid.uuid4()
    original_key = f"users/{user_id}/wardrobe/input.jpg"
    original_url = f"{_BASE_URL}/files/{original_key}"
    image = Image.new("RGB", (60, 100), "red")
    image.paste("blue", (0, 50, 60, 100))
    with io.BytesIO() as buffer:
        image.save(buffer, "JPEG")
        store.objects[original_key] = buffer.getvalue()

    candidates = [
        {
            "temp_item_id": "top",
            "title": "Red shirt",
            "category": "top",
            "color_main": "red",
            "confidence_score": 0.9,
            "original_image_url": original_url,
            "bbox": [0.1, 0.1, 0.8, 0.4],
        }
    ]
    if include_bottom:
        candidates.append({
            "temp_item_id": "bottom",
            "title": "Blue pants",
            "category": "bottom",
            "color_main": "blue",
            "confidence_score": 0.9,
            "original_image_url": original_url,
            "bbox": [0.1, 0.6, 0.8, 0.9],
        })
    version = source.source_version(original_key, original_url)
    async with factory() as session:
        session.add(User(id=user_id))
        await session.flush()
        task = ImportTask(
            id=task_id, user_id=user_id, input_type="outfit_image", source_type="album",
            task_status=ImportStatus.WAITING_CONFIRM.value,
            input_payload={"image_url": original_url, "source_object_key": original_key, "source_version": version},
        )
        store_candidates(task, candidates)
        session.add(task)
        await session.flush()
        for candidate in candidates:
            await garment_crops.enqueue_garment_crop(session, task, str(candidate["temp_item_id"]), version, None)
        await session.commit()
    entries = [
        ConfirmItemInput(
            str(candidate["temp_item_id"]), str(candidate["title"]),
            str(candidate["category"]), str(candidate["color_main"]),
        )
        for candidate in candidates
    ]
    return RecognizedImport(user_id, task_id, original_key, original_url, entries)


async def _confirm(
    factory: async_sessionmaker[AsyncSession], recognized: RecognizedImport,
) -> list[ConfirmedEntry]:
    async with factory() as session:
        entries = await ConfirmImageImportTask(
            SqlAlchemyImportTaskRepository(session), SqlAlchemyWardrobeRepository(session),
            SqlAlchemyRecommendationRepository(session),
        ).confirm_items(recognized.task_id, recognized.user_id, recognized.entries)
        await session.commit()
        return entries


async def _crop_jobs(
    factory: async_sessionmaker[AsyncSession], recognized: RecognizedImport,
) -> dict[str, AsyncJob]:
    jobs: dict[str, AsyncJob] = {}
    async with factory() as session:
        for _ in recognized.entries:
            job = await claim_one(session, [garment_crops.CROP_JOB_TYPE])
            assert job is not None and job.target_id == recognized.task_id
            jobs[job.payload["candidate_id"]] = job
    return jobs


async def _assert_confirmed_sources(
    factory: async_sessionmaker[AsyncSession],
    recognized: RecognizedImport,
    store: CropStore,
    mapping: dict[str, uuid.UUID],
) -> None:
    async with factory() as session:
        task = await session.get(ImportTask, recognized.task_id)
        assert task is not None and task.task_status == ImportStatus.COMPLETED.value
        assert confirmed_item_ids(task) == {candidate_id: str(item_id) for candidate_id, item_id in mapping.items()}
        crops = {candidate["temp_item_id"]: candidate for candidate in normalize_candidates(task)}
        assert len({candidate["crop_object_key"] for candidate in crops.values()}) == len(mapping)
        assert len(mapping) == len(set(mapping.values())) == len(recognized.entries)
        assert set(crops) == set(mapping)

        item_ids = set(mapping.values())
        rows = list(await session.scalars(select(WardrobeItem).where(WardrobeItem.user_id == recognized.user_id)))
        assert {item.id for item in rows} == item_ids
        items = {item.id: item for item in rows}
        images = list(await session.scalars(select(WardrobeItemImage).where(WardrobeItemImage.item_id.in_(item_ids))))
        jobs = list(await session.scalars(
            select(AsyncJob).where(AsyncJob.target_id.in_(item_ids), AsyncJob.job_type.in_(("embedding", "cutout")))
        ))
        holds = list(await session.scalars(
            select(PaidGenerationReservation).where(
                PaidGenerationReservation.user_id == recognized.user_id, PaidGenerationReservation.job_type == "cutout"
            )
        ))
        assert len(images) == 3 * len(item_ids)
        assert len(jobs) == 2 * len(item_ids)
        assert len(holds) == len(item_ids)
        for candidate_id, item_id in mapping.items():
            item = items[item_id]
            key = crops[candidate_id]["crop_object_key"]
            url = crops[candidate_id]["crop_image_url"]
            assert item.original_image_url == recognized.original_url
            assert item.cover_image_url == url
            assert item.embedding_source_version == source.source_version(key, url)
            assert item.cutout_status == CutoutStatus.QUEUED.value
            assert {
                (image.image_type, image.object_key)
                for image in images if image.item_id == item_id
            } == {
                (ImageType.ORIGINAL.value, recognized.original_key),
                (ImageType.STANDARD.value, key),
                (ImageType.CROP.value, key),
            }

            with Image.open(io.BytesIO(store.objects[key])) as crop:
                red, _, blue = crop.convert("RGB").getpixel((crop.width // 2, crop.height // 2))
            assert (red > blue) == (candidate_id == "top")

            for kind, payload_key in (("embedding", "image_key"), ("cutout", "source_key")):
                job, = [job for job in jobs if job.target_id == item_id and job.job_type == kind]
                assert job.status == JOB_STATUS_QUEUED
                assert job.payload[payload_key] == key
                assert job.payload["source_version"] == item.embedding_source_version
                if kind == "cutout":
                    held, = [hold for hold in holds if hold.job_id == job.id]
                    assert held.settled_at is None and held.target_id is None


@pytest.mark.parametrize("first_lock", ["crop", "confirm"])
async def test_confirm_and_crop_share_task_lock_and_prepare_each_item_once(
    crop_environment: tuple[async_sessionmaker[AsyncSession], CropStore],
    monkeypatch: pytest.MonkeyPatch,
    first_lock: str,
) -> None:
    factory, store = crop_environment
    recognized = await _recognized_import(factory, store, include_bottom=True)
    crop_jobs = await _crop_jobs(factory, recognized)
    # The bottom is a distinct candidate that is already cropped in both
    # orderings. Its image still needs to survive the top crop's late merge.
    await garment_crops._process_crop(crop_jobs["bottom"])

    holder_locked = asyncio.Event()
    contender_ready = asyncio.Event()
    release_holder = asyncio.Event()
    backend_pids: dict[str, int] = {}
    original_get = SqlAlchemyImportTaskRepository.get_for_user
    contender = "confirm" if first_lock == "crop" else "crop"

    async def get_with_lock_barrier(
        self: SqlAlchemyImportTaskRepository, task_id: uuid.UUID, user_id: uuid.UUID, *, for_update: bool = False,
    ) -> ImportTask | None:
        current = asyncio.current_task()
        actor = current.get_name() if current is not None else ""
        if not for_update or task_id != recognized.task_id or actor not in {first_lock, contender}:
            return await original_get(self, task_id, user_id, for_update=for_update)

        backend_pids[actor] = await self.session.scalar(select(func.pg_backend_pid()))
        if actor == contender:
            contender_ready.set()
        task = await original_get(self, task_id, user_id, for_update=True)
        if actor == first_lock:
            holder_locked.set()
            await release_holder.wait()
        return task

    async def crop() -> None:
        await garment_crops._process_crop(crop_jobs["top"])

    monkeypatch.setattr(SqlAlchemyImportTaskRepository, "get_for_user", get_with_lock_barrier)
    actors = {"crop": crop, "confirm": lambda: _confirm(factory, recognized)}
    async with asyncio.timeout(10), asyncio.TaskGroup() as group:
        running = {first_lock: group.create_task(actors[first_lock](), name=first_lock)}
        await holder_locked.wait()
        running[contender] = group.create_task(actors[contender](), name=contender)
        await contender_ready.wait()
        async with factory() as observer:
            # Wait for actual lock contention reported by Postgres, rather
            # than inferring blocking from scheduled Python coroutines.
            while not await observer.scalar(
                text("SELECT :holder = ANY(pg_blocking_pids(:contender))"),
                {"holder": backend_pids[first_lock], "contender": backend_pids[contender]},
            ):
                assert not running[contender].done()
        release_holder.set()

    created = running["confirm"].result()
    assert created is not None and all(entry.created for entry in created)
    mapping = {entry.temp_item_id: entry.item.id for entry in created}
    replay = await _confirm(factory, recognized)
    assert {entry.temp_item_id: entry.item.id for entry in replay} == mapping
    assert all(not entry.created for entry in replay)
    await _assert_confirmed_sources(factory, recognized, store, mapping)


async def test_soft_delete_while_crop_upload_is_blocked_never_restores_item(
    crop_environment: tuple[async_sessionmaker[AsyncSession], CropStore],
) -> None:
    factory, store = crop_environment
    recognized = await _recognized_import(factory, store, include_bottom=False)
    created, = await _confirm(factory, recognized)
    item_id = created.item.id
    job = (await _crop_jobs(factory, recognized))["top"]
    store.uploading, store.resume_upload = asyncio.Event(), asyncio.Event()

    async with asyncio.timeout(10), asyncio.TaskGroup() as group:
        group.create_task(garment_crops._process_crop(job))
        await store.uploading.wait()
        async with factory() as session:
            wardrobe = SqlAlchemyWardrobeRepository(session)
            use_case = DeleteWardrobeItem(wardrobe, SqlAlchemyRecommendationRepository(session))
            assert await use_case.execute(item_id, recognized.user_id)
            deleted = await session.get(WardrobeItem, item_id)
            assert deleted is not None and deleted.deleted_at is not None
            deleted_at = deleted.deleted_at
            await session.commit()
        store.resume_upload.set()

    async with factory() as session:
        item = await session.get(WardrobeItem, item_id)
        assert item is not None and item.deleted_at == deleted_at
        assert item.cover_image_url == item.original_image_url == recognized.original_url
        assert item.embedding_source_version is None and item.cutout_status == CutoutStatus.SKIPPED.value
        assert await SqlAlchemyWardrobeRepository(session).get_for_user(item_id, recognized.user_id) is None
        job_row = await session.get(AsyncJob, job.id)
        assert job_row is not None and job_row.status == JOB_STATUS_SUCCEEDED
        assert any(key != recognized.original_key for key in store.objects)
        images = list(await session.scalars(select(WardrobeItemImage).where(WardrobeItemImage.item_id == item_id)))
        assert {(image.image_type, image.object_key) for image in images} == {
            (ImageType.ORIGINAL.value, recognized.original_key),
            (ImageType.STANDARD.value, recognized.original_key),
        }
        assert not list(await session.scalars(select(AsyncJob.id).where(AsyncJob.target_id == item_id)))
        assert await session.scalar(
            select(PaidGenerationReservation.id).where(PaidGenerationReservation.user_id == recognized.user_id)
        ) is None
