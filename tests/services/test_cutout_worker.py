"""Item cutout worker integration tests.

Cover:
- enqueue idempotency (per-item key)
- drain happy path: claim → cutout (fake provider) → item cover + standard
  image show the cutout, the original photo stays, cutout_status='ready'
- invalid payload surfaces as a structured failure, not a crash
- dead-letter reconcile flips a stuck queued item to 'failed'
"""

from __future__ import annotations

import asyncio
import io
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.cost_guard import usage_today
from app.application.import_tasks import prepare_derivatives
from app.application.paid_generation_admission import settle_terminal_reservations
from app.application.ports.image_cutout import CutoutResult
from app.common.enums import CutoutStatus, ImageType
from app.config.settings import get_settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.paid_generation import PaidGenerationReservation
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
    claim_one,
    enqueue,
    recover_expired_leases,
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

    async def _fake_load(
        source_url: str | None, source_key: str | None, *, user_id: uuid.UUID | None = None
    ) -> bytes:
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
        ) -> CutoutResult:
            hints.append((title, color))
            if accepted:
                return CutoutResult("succeeded", image_bytes, provider_cost_cents=2)
            return CutoutResult("quality_rejected")

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
        assert item.cutout_status == (CutoutStatus.READY.value if accepted else CutoutStatus.REJECTED.value)
        key = f"users/{user_id}/items/{item_id}/cutout/{job_id}/1.png"
        expected_cover = f"https://fake-storage/{key}" if accepted else "https://x/original.jpg"
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
        assert standard.object_key == (key if accepted else None)


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


async def test_legacy_unverified_crop_is_never_used_for_a_product_shot(
    patched_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def unexpected_fetch(*args: object, **kwargs: object) -> bytes:
        pytest.fail("a legacy unverified crop must not reach storage or the product-shot provider")

    monkeypatch.setattr(cutout_worker, "_load_source_bytes", unexpected_fetch)
    user_id, item_id = await _seed_item(patched_session_factory)
    async with patched_session_factory() as session:
        key = f"users/{user_id}/wardrobe/crops/legacy.jpg"
        job, _ = await enqueue_item_cutout(
            session, item_id=item_id, user_id=user_id,
            source_url=f"https://fake-storage/{key}", source_key=key, category="top",
        )
        await session.commit()
        job_id = job.id

    assert await drain_cutout_jobs(ctx=None) == 1

    async with patched_session_factory() as session:
        item = await session.get(WardrobeItem, item_id)
        assert item is not None
        assert item.cutout_status == CutoutStatus.SKIPPED.value
        assert item.cover_image_url == "https://x/original.jpg"
        final = await session.get(AsyncJob, job_id)
        assert final is not None
        assert final.result == {"outcome": "not_called", "provider_cost_cents": 0}
        assert await usage_today(session, user_id, JOB_TYPE) == 0


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
    from app.application.import_tasks.confirm_import import ConfirmImageImportTask
    from app.common.enums import ImportStatus
    from app.infrastructure.persistence.models.import_task import ImportTask
    from app.infrastructure.persistence.repositories.import_task_repo import (
        SqlAlchemyImportTaskRepository,
    )
    from app.infrastructure.persistence.repositories.wardrobe_repo import (
        SqlAlchemyWardrobeRepository,
    )

    base = get_settings()
    monkeypatch.setattr(
        prepare_derivatives, "get_settings", lambda: base.model_copy(update={"ENABLE_ITEM_CUTOUT": True})
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


async def test_concurrent_admission_holds_queued_and_legacy_budget(
    patched_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = get_settings().model_copy(update={
        "ENABLE_ITEM_CUTOUT": True, "ENABLE_COST_GUARD": True, "ITEM_CUTOUT_DAILY_LIMIT": 2,
    })
    monkeypatch.setattr(prepare_derivatives, "get_settings", lambda: settings)
    monkeypatch.setattr("app.application.cost_guard.get_settings", lambda: settings)
    user_id, legacy_item_id = await _seed_item(patched_session_factory)
    async with patched_session_factory() as s:
        legacy, _ = await enqueue_item_cutout(
            s,
            item_id=legacy_item_id,
            user_id=user_id,
            source_url="https://x/original.jpg",
            category="top",
        )
        legacy.created_at = datetime.now(UTC) - timedelta(days=2)
        await s.commit()

    async def confirm_garment(index: int) -> uuid.UUID:
        async with patched_session_factory() as s:
            key = f"users/{user_id}/wardrobe/verified-crops/{index}.jpg"
            item = WardrobeItem(user_id=user_id, category="top", title=f"Item {index}", color_main="white")
            s.add(item)
            await s.flush()
            await prepare_derivatives.prepare_item_derivatives(
                s, item, source_key=key, source_url=None, source_version=f"image-{index}",
            )
            await s.commit()
            return item.id

    item_ids = await asyncio.gather(*(confirm_garment(i) for i in range(4)))
    async with patched_session_factory() as s:
        items = (await s.scalars(select(WardrobeItem).where(WardrobeItem.id.in_(item_ids)))).all()
        assert sorted(item.cutout_status for item in items) == ["queued", "skipped", "skipped", "skipped"]
        admitted = next(item for item in items if item.cutout_status == CutoutStatus.QUEUED.value)
        await prepare_derivatives.prepare_item_derivatives(
            s, admitted, source_key=f"users/{user_id}/wardrobe/verified-crops/replayed.jpg",
            source_url=None, source_version=admitted.embedding_source_version or "",
        )
        reservations = (
            await s.scalars(select(PaidGenerationReservation).where(PaidGenerationReservation.user_id == user_id))
        ).all()
        assert len(reservations) == 1
        assert reservations[0].target_id is None
        job = await s.get(AsyncJob, reservations[0].job_id)
        assert job is not None and job.target_id == admitted.id
        assert await usage_today(s, user_id, JOB_TYPE) == 2


@pytest.mark.parametrize(
    ("outcome", "settled", "usage"),
    [
        ("succeeded", "succeeded", 1),
        ("quality_rejected", "quality_rejected", 1),
        ("not_called", "abandoned", 0),
        ("technical_failure", "technical_failure", 0),
    ],
)
async def test_reservation_settles_cutout_outcomes(
    patched_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    outcome: str,
    settled: str,
    usage: int,
) -> None:
    _patch_render_deps(monkeypatch)
    settings = get_settings().model_copy(update={"ENABLE_ITEM_CUTOUT": True})
    monkeypatch.setattr(prepare_derivatives, "get_settings", lambda: settings)
    user_id, item_id = await _seed_item(patched_session_factory)
    async with patched_session_factory() as s:
        item = await s.get(WardrobeItem, item_id)
        assert item is not None
        await prepare_derivatives.prepare_item_derivatives(
            s, item, source_key=f"users/{user_id}/wardrobe/source.png", source_url=None, source_version="v1",
        )
        job = await s.scalar(select(AsyncJob).where(AsyncJob.job_type == JOB_TYPE, AsyncJob.target_id == item_id))
        assert job is not None
        job.max_attempts = 1
        job_id = job.id
        assert await usage_today(s, user_id, JOB_TYPE) == 1
        await s.commit()

    calls: list[bytes] = []

    class Provider:
        async def extract(
            self, image: bytes, category: str, *, title: str | None, color: str | None,
        ) -> CutoutResult:
            calls.append(image)
            if outcome == "technical_failure":
                raise RuntimeError("provider credentials must never enter job.result")
            if outcome == "quality_rejected":
                return CutoutResult("quality_rejected")
            return CutoutResult("succeeded", image, provider_cost_cents=3)

    monkeypatch.setattr("app.bootstrap.container.image_cutout_service", Provider)
    if outcome == "not_called":
        async with patched_session_factory() as s:
            item = await s.get(WardrobeItem, item_id)
            assert item is not None
            item.deleted_at = datetime.now(UTC)
            await s.commit()

    assert await drain_cutout_jobs(ctx=None) == 1
    async with patched_session_factory() as s:
        reservation = await s.scalar(
            select(PaidGenerationReservation).where(PaidGenerationReservation.job_id == job_id)
        )
        assert reservation is not None and reservation.outcome == settled
        assert await usage_today(s, user_id, JOB_TYPE) == usage
        assert await settle_terminal_reservations(s, job_type=JOB_TYPE) == 0
        job = await s.get(AsyncJob, job_id)
        assert job is not None and job.result is not None
        assert job.result["outcome"] == outcome
        assert job.result["provider_cost_cents"] == {"succeeded": 3, "not_called": 0}.get(outcome)
    assert len(calls) == (0 if outcome == "not_called" else 1)


async def test_late_cutout_attempt_cannot_overwrite_current_object(
    patched_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_render_deps(monkeypatch)
    user_id, item_id = await _seed_item(patched_session_factory)
    files: dict[str, bytes] = {}

    class Storage:
        async def put_object(self, key: str, data: bytes, content_type: str) -> str:
            files[key] = data
            return f"https://fake-storage/{key}"

    class ReclaimingProvider:
        calls = 0

        async def extract(
            self, image: bytes, category: str, *, title: str | None, color: str | None,
        ) -> CutoutResult:
            self.calls += 1
            if self.calls == 1:
                async with patched_session_factory() as s:
                    row = await s.get(AsyncJob, job_id)
                    assert row is not None
                    row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
                    await s.flush()
                    assert await recover_expired_leases(s) == 1
                    await s.commit()
                assert await drain_cutout_jobs(ctx=None) == 1
                return CutoutResult("succeeded", b"old")
            return CutoutResult("succeeded", b"current")

    provider = ReclaimingProvider()
    monkeypatch.setattr("app.bootstrap.container.image_cutout_service", lambda: provider)
    monkeypatch.setattr("app.bootstrap.container.object_storage", Storage)
    async with patched_session_factory() as s:
        job, _ = await enqueue_item_cutout(
            s, item_id=item_id, user_id=user_id, source_url="https://x/original.jpg", category="top"
        )
        job_id = job.id
        await s.commit()
        claimed = await claim_one(s, [JOB_TYPE])
    assert claimed is not None
    await cutout_worker._process_one(claimed)
    async with patched_session_factory() as s:
        item = await s.get(WardrobeItem, item_id)
        assert item is not None
        winning_key = f"users/{user_id}/items/{item_id}/cutout/{job_id}/2.png"
        assert item.cover_image_url == f"https://fake-storage/{winning_key}"
        assert files[winning_key] == b"current"
        assert files[f"users/{user_id}/items/{item_id}/cutout/{job_id}/1.png"] == b"old"


async def test_cutout_drain_keeps_remaining_jobs_unclaimed_when_budget_is_short(
    patched_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_render_deps(monkeypatch)
    settings = get_settings().model_copy(update={
        "ARQ_JOB_TIMEOUT_SECONDS": 300, "IMAGE_CUTOUT_TIMEOUT_SECONDS": 180, "VISION_TIMEOUT_SECONDS": 10,
    })
    monkeypatch.setattr(cutout_worker, "get_settings", lambda: settings)
    clock = iter([0.0, 0.0, 75.0])
    monkeypatch.setattr(cutout_worker, "monotonic", lambda: next(clock))
    seeded = [await _seed_item(patched_session_factory) for _ in range(2)]
    async with patched_session_factory() as s:
        for user_id, item_id in seeded:
            await enqueue_item_cutout(
                s, item_id=item_id, user_id=user_id, source_url="https://x/original.jpg", category="top"
            )
        await s.commit()
    assert await drain_cutout_jobs(ctx=None) == 1
    async with patched_session_factory() as s:
        jobs = (await s.scalars(select(AsyncJob).where(AsyncJob.job_type == JOB_TYPE))).all()
        assert sorted((job.status, job.attempts) for job in jobs) == [("queued", 0), ("succeeded", 1)]


async def test_cancelled_cutout_claim_requeues_with_backoff(
    patched_session_factory: async_sessionmaker[AsyncSession], monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, item_id = await _seed_item(patched_session_factory)
    started = asyncio.Event()
    blocked = asyncio.Event()

    async def blocking_provider(job: AsyncJob) -> None:
        started.set()
        await blocked.wait()

    monkeypatch.setattr(cutout_worker, "_process_one", blocking_provider)
    async with patched_session_factory() as s:
        job, _ = await enqueue_item_cutout(
            s, item_id=item_id, user_id=user_id, source_url="https://x/original.jpg", category="top"
        )
        await s.commit()
        job_id = job.id

    drain = asyncio.create_task(drain_cutout_jobs(ctx=None))
    await asyncio.wait_for(started.wait(), timeout=5)
    drain.cancel()
    with pytest.raises(asyncio.CancelledError):
        await drain
    async with patched_session_factory() as s:
        finalized = await s.get(AsyncJob, job_id)
        assert finalized is not None and finalized.status == "queued" and finalized.attempts == 1
        assert finalized.lease_expires_at is None
        assert finalized.next_run_at > datetime.now(UTC)
