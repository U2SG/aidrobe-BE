"""Item cutout worker integration tests.

Cover:
- enqueue idempotency (per-item key)
- drain happy path: claim → cutout (fake provider) → item cover + standard
  image show the cutout, the original photo stays, cutout_status='ready'
- invalid payload surfaces as a structured failure, not a crash
- dead-letter reconcile flips a stuck queued item to 'failed'
"""

from __future__ import annotations

import io
import uuid
from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.common.enums import CutoutStatus, ImageType
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem, WardrobeItemImage
from app.infrastructure.workers import cutout_worker
from app.infrastructure.workers.cutout_worker import (
    JOB_TYPE,
    drain_cutout_jobs,
    enqueue_item_cutout,
    reconcile_dead_cutout_items,
)
from app.infrastructure.workers.job_runtime import (
    JOB_STATUS_QUEUED,
    JOB_STATUS_SUCCEEDED,
    enqueue,
)


@pytest_asyncio.fixture
async def patched_session_factory(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(postgres_url, future=True)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(cutout_worker, "AsyncSessionLocal", factory)
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
    Image.new("RGB", (64, 96), (120, 125, 120)).save(buf, "PNG")
    return buf.getvalue()


def _patch_render_deps(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep image reads and writes in memory."""
    monkeypatch.setattr("app.bootstrap.container.object_storage", lambda: _FakeStorage())

    async def _fake_load(source_url: str, source_key: str | None) -> bytes:
        return _png_bytes()

    monkeypatch.setattr(cutout_worker, "_load_source_bytes", _fake_load)


async def _seed_item(
    factory: async_sessionmaker[AsyncSession],
    *,
    cutout_status: str = CutoutStatus.QUEUED.value,
    with_standard: bool = True,
) -> tuple[uuid.UUID, uuid.UUID]:
    user_id = uuid.uuid4()
    async with factory() as s:
        s.add(User(id=user_id))
        await s.flush()
        item = WardrobeItem(
            user_id=user_id,
            title="Off-Shoulder Top",
            category="top",
            color_main="grey",
            cover_image_url="https://x/original.jpg",
            original_image_url="https://x/original.jpg",
            cutout_status=cutout_status,
        )
        s.add(item)
        await s.flush()
        item_id = item.id
        if with_standard:
            from datetime import UTC, datetime

            s.add(
                WardrobeItemImage(
                    item_id=item_id,
                    image_type=ImageType.STANDARD.value,
                    image_url="https://x/original.jpg",
                    created_at=datetime.now(UTC),
                )
            )
        await s.commit()
    return user_id, item_id


async def test_enqueue_idempotent_per_item(
    patched_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id, item_id = await _seed_item(patched_session_factory)
    async with patched_session_factory() as s:
        first, ins_first = await enqueue_item_cutout(
            s, item_id=item_id, user_id=user_id, source_url="https://x/o.jpg", category="top"
        )
        second, ins_second = await enqueue_item_cutout(
            s, item_id=item_id, user_id=user_id, source_url="https://x/o.jpg", category="top"
        )
        await s.commit()
    assert ins_first is True
    assert ins_second is False
    assert first.id == second.id
    assert first.idempotency_key == f"cutout:{item_id}"


@pytest.mark.parametrize("accepted", [True, False])
async def test_drain_applies_only_accepted_product_photos(
    patched_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    accepted: bool,
) -> None:
    _patch_render_deps(monkeypatch)
    hints: list[tuple[str | None, str | None]] = []

    class RecordingCutout:
        async def extract(
            self, image_bytes: bytes, category: str, *, title: str | None = None, color: str | None = None
        ) -> bytes | None:
            hints.append((title, color))
            return image_bytes if accepted else None

    monkeypatch.setattr("app.bootstrap.container.image_cutout_service", RecordingCutout)
    user_id, item_id = await _seed_item(patched_session_factory)
    async with patched_session_factory() as s:
        job, _ = await enqueue_item_cutout(
            s,
            item_id=item_id,
            user_id=user_id,
            source_url="https://x/o.jpg",
            category="top",
            title="White undershirt",
            color="white",
        )
        await s.commit()
        job_id = job.id

    processed = await drain_cutout_jobs(ctx=None)
    assert processed == 1
    assert hints == [("White undershirt", "white")]

    async with patched_session_factory() as s:
        finalized = await s.get(AsyncJob, job_id)
        assert finalized is not None
        assert finalized.status == JOB_STATUS_SUCCEEDED

        item = await s.get(WardrobeItem, item_id)
        assert item is not None
        assert item.cutout_status == (CutoutStatus.READY.value if accepted else CutoutStatus.SKIPPED.value)
        expected_cover = f"https://fake-storage/{user_id}/items/{item_id}/cutout.png" if accepted else "https://x/original.jpg"
        assert item.cover_image_url == expected_cover
        assert item.original_image_url == "https://x/original.jpg"

        standard = (
            await s.execute(
                select(WardrobeItemImage).where(
                    WardrobeItemImage.item_id == item_id,
                    WardrobeItemImage.image_type == ImageType.STANDARD.value,
                )
            )
        ).scalar_one()
        assert standard.image_url == expected_cover
        assert standard.object_key == (f"{user_id}/items/{item_id}/cutout.png" if accepted else None)


async def test_drain_skips_invalid_payload(
    patched_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    async with patched_session_factory() as s:
        job, _ = await enqueue(
            s,
            job_type=JOB_TYPE,
            idempotency_key=f"cutout:bad:{uuid.uuid4()}",
            payload={},  # missing item_id / source_url
        )
        await s.commit()
        job_id = job.id

    processed = await drain_cutout_jobs(ctx=None)
    assert processed == 1

    async with patched_session_factory() as s:
        final = await s.get(AsyncJob, job_id)
        assert final is not None
        assert final.status == JOB_STATUS_QUEUED
        assert final.last_error == "missing payload fields"


async def test_reconcile_dead_marks_item_failed(
    patched_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id, item_id = await _seed_item(patched_session_factory)
    async with patched_session_factory() as s:
        # A dead cutout job targeting the still-queued item.
        s.add(
            AsyncJob(
                job_type=JOB_TYPE,
                idempotency_key=f"cutout:{item_id}",
                status="dead",
                target_id=item_id,
                user_id=user_id,
                payload={"item_id": str(item_id)},
            )
        )
        await s.commit()

    count = await reconcile_dead_cutout_items(ctx=None)
    assert count == 1

    async with patched_session_factory() as s:
        item = await s.get(WardrobeItem, item_id)
        assert item is not None
        assert item.cutout_status == CutoutStatus.FAILED.value


@pytest.mark.parametrize(("input_type", "enqueued"), [("image", True), ("outfit_image", False)])
async def test_confirm_enqueues_cutout_only_with_usable_source(
    patched_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    input_type: str,
    enqueued: bool,
) -> None:
    """Confirm-import enqueues a cutout job + flips the item to 'queued' only
    when ENABLE_ITEM_CUTOUT is on. Flag is monkeypatched on the consuming
    module (get_settings is lru_cached, so the dep override path won't work)."""
    from app.application.import_tasks import confirm_import as ci_mod
    from app.application.import_tasks.confirm_import import ConfirmImageImportTask
    from app.common.enums import ImportStatus
    from app.infrastructure.persistence.models.import_task import ImportTask
    from app.infrastructure.persistence.repositories.import_task_repo import (
        SqlAlchemyImportTaskRepository,
    )
    from app.infrastructure.persistence.repositories.wardrobe_repo import (
        SqlAlchemyWardrobeRepository,
    )

    base = ci_mod.get_settings()
    monkeypatch.setattr(
        ci_mod, "get_settings", lambda: base.model_copy(update={"ENABLE_ITEM_CUTOUT": True})
    )

    user_id = uuid.uuid4()
    async with patched_session_factory() as s:
        s.add(User(id=user_id))
        await s.flush()
        task = ImportTask(
            user_id=user_id,
            input_type=input_type,
            source_type="album",
            task_status=ImportStatus.WAITING_CONFIRM.value,
            input_payload={"image_url": "https://x/o.jpg"},
            candidate_payload={
                "original_image_url": "https://x/o.jpg",
                "category": "top",
                "title": "Off-Shoulder Top",
            },
        )
        s.add(task)
        await s.flush()
        task_id = task.id
        await s.commit()

    async with patched_session_factory() as s:
        use_case = ConfirmImageImportTask(
            SqlAlchemyImportTaskRepository(s), SqlAlchemyWardrobeRepository(s)
        )
        confirmed = await use_case.execute(
            task_id=task_id,
            user_id=user_id,
            title="Off-Shoulder Top",
            category="top",
            color_main="grey",
        )
        await s.commit()
        item_id = confirmed.item.id

    async with patched_session_factory() as s:
        refreshed = await s.get(WardrobeItem, item_id)
        assert refreshed is not None
        assert refreshed.cutout_status == (CutoutStatus.QUEUED.value if enqueued else CutoutStatus.SKIPPED.value)
        job = (
            await s.execute(
                select(AsyncJob).where(
                    AsyncJob.job_type == JOB_TYPE, AsyncJob.target_id == item_id
                )
            )
        ).scalar_one_or_none()
        if enqueued:
            assert job is not None
            assert job.idempotency_key == f"cutout:{item_id}"
        else:
            assert job is None


def test_drain_constants_are_sane() -> None:
    assert 1 <= cutout_worker.MAX_DRAIN_PER_TICK <= 50
    assert cutout_worker.JOB_TYPE == "cutout"
