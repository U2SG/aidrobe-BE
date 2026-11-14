"""C2 — embedding worker integration tests.

Cover:
- enqueue idempotency on (item_id, model_version)
- drain happy path: claim → embed → persist ItemEmbedding → mark_succeeded
- embed exception → mark_failed (retry)
- model_version mismatch → mark_failed (no corrupt vector)
- payload-shape validation
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.common.enums import Category, ItemStatus
from app.infrastructure.embedding.fake_embedding import FakeEmbedding
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.embedding import ItemEmbedding, is_current_embedding
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.workers import embedding_worker
from app.infrastructure.workers.embedding_worker import (
    JOB_TYPE,
    drain_embedding_jobs,
    enqueue_embedding,
)
from app.infrastructure.workers.job_runtime import (
    JOB_STATUS_DEAD,
    JOB_STATUS_QUEUED,
    JOB_STATUS_SUCCEEDED,
    claim_one,
    enqueue,
)


@pytest_asyncio.fixture
async def patched_session_factory(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    """The embedding worker uses module-level `AsyncSessionLocal`. Point it at
    the testcontainers DB so drain_embedding_jobs talks to the same Postgres
    the test fixture set up."""
    engine = create_async_engine(postgres_url, future=True)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(embedding_worker, "AsyncSessionLocal", factory)
    try:
        yield factory
    finally:
        await engine.dispose()


async def _seed_item(factory: async_sessionmaker[AsyncSession]) -> tuple[uuid.UUID, uuid.UUID]:
    user_id = uuid.uuid4()
    async with factory() as s:
        s.add(User(id=user_id))
        await s.flush()
        item = WardrobeItem(
            user_id=user_id,
            title="White shirt",
            category=Category.TOP.value,
            color_main="white",
            status=ItemStatus.WEARABLE.value,
            embedding_source_version="image-v1",
        )
        s.add(item)
        await s.flush()
        item_id = item.id
        await s.commit()
    return user_id, item_id


async def test_enqueue_embedding_is_idempotent_on_item_model_and_source(
    patched_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    user_id, item_id = await _seed_item(patched_session_factory)
    async with patched_session_factory() as s:
        first, inserted_first = await enqueue_embedding(
            s,
            item_id=item_id,
            image_url="https://example.com/a.jpg",
            model_version="fake:512",
            source_version="image-v1",
            user_id=user_id,
        )
        second, inserted_second = await enqueue_embedding(
            s,
            item_id=item_id,
            image_url="https://example.com/different.jpg",
            model_version="fake:512",
            source_version="image-v1",
            user_id=user_id,
        )
        await s.commit()

    assert inserted_first is True
    assert inserted_second is False
    assert first.id == second.id
    assert first.payload == second.payload  # second enqueue must NOT overwrite

    # Different model_version must produce a separate job.
    async with patched_session_factory() as s:
        third, inserted_third = await enqueue_embedding(
            s,
            item_id=item_id,
            image_url="https://example.com/a.jpg",
            model_version="clip:ViT-B-32:openai",
            source_version="image-v1",
            user_id=user_id,
        )
        await s.commit()
    assert inserted_third is True
    assert third.id != first.id

    async with patched_session_factory() as s:
        fourth, inserted_fourth = await enqueue_embedding(
            s,
            item_id=item_id,
            image_url="https://example.com/a.jpg",
            model_version="fake:512",
            source_version="image-v2",
            user_id=user_id,
        )
    assert inserted_fourth is True
    assert fourth.id != first.id


async def test_drain_processes_job_writes_vector_and_marks_succeeded(
    patched_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("app.bootstrap.container.embedding_service", lambda: FakeEmbedding())
    user_id, item_id = await _seed_item(patched_session_factory)

    async with patched_session_factory() as s:
        job, _ = await enqueue_embedding(
            s,
            item_id=item_id,
            image_url="https://example.com/x.jpg",
            model_version="fake:512",
            source_version="image-v1",
            user_id=user_id,
        )
        await s.commit()
        job_id = job.id

    processed = await drain_embedding_jobs(ctx=None)
    assert processed == 1

    async with patched_session_factory() as s:
        # ItemEmbedding row written.
        rows = (await s.execute(select(ItemEmbedding).where(ItemEmbedding.item_id == item_id))).scalars().all()
        assert len(rows) == 1
        assert rows[0].model_name == "fake:512"
        assert rows[0].source_version == "image-v1"
        assert isinstance(rows[0].vector, list)
        assert len(rows[0].vector) == FakeEmbedding.DIM

        # Job is succeeded.
        finalized = await s.get(AsyncJob, job_id)
        assert finalized is not None
        assert finalized.status == JOB_STATUS_SUCCEEDED
        assert finalized.lease_expires_at is None


async def test_drain_marks_failed_when_embed_raises(
    patched_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ExplodingEmbedder:
        model_version = "fake:512"

        async def embed_image(self, image_url: str) -> list[float]:
            raise RuntimeError("boom")

    monkeypatch.setattr("app.bootstrap.container.embedding_service", lambda: ExplodingEmbedder())
    user_id, item_id = await _seed_item(patched_session_factory)

    async with patched_session_factory() as s:
        job, _ = await enqueue_embedding(
            s,
            item_id=item_id,
            image_url="https://example.com/x.jpg",
            model_version="fake:512",
            source_version="image-v1",
            user_id=user_id,
        )
        await s.commit()
        job_id = job.id

    processed = await drain_embedding_jobs(ctx=None)
    assert processed == 1

    async with patched_session_factory() as s:
        finalized = await s.get(AsyncJob, job_id)
        assert finalized is not None
        # First failure → back to queued with backoff (max_attempts default 3).
        assert finalized.status == JOB_STATUS_QUEUED
        assert finalized.attempts == 1
        assert finalized.last_error == "embedding_provider_failed"
        # No ItemEmbedding row should be persisted on failure.
        rows = (await s.execute(select(ItemEmbedding).where(ItemEmbedding.item_id == item_id))).scalars().all()
        assert rows == []


async def test_drain_rejects_model_version_mismatch(
    patched_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A job enqueued for `clip:...` must NOT be processed by a `fake:512`
    embedder — that would silently corrupt search."""
    monkeypatch.setattr("app.bootstrap.container.embedding_service", lambda: FakeEmbedding())
    user_id, item_id = await _seed_item(patched_session_factory)

    async with patched_session_factory() as s:
        job, _ = await enqueue_embedding(
            s,
            item_id=item_id,
            image_url="https://example.com/x.jpg",
            model_version="clip:ViT-B-32:openai",  # mismatched
            source_version="image-v1",
            user_id=user_id,
        )
        await s.commit()
        job_id = job.id

    await drain_embedding_jobs(ctx=None)

    async with patched_session_factory() as s:
        finalized = await s.get(AsyncJob, job_id)
        assert finalized is not None
        # Treated as a normal failure — backoff applies, eventually dead.
        assert finalized.status == JOB_STATUS_QUEUED
        assert finalized.last_error and "model_version mismatch" in finalized.last_error
        # No vector persisted.
        rows = (await s.execute(select(ItemEmbedding).where(ItemEmbedding.item_id == item_id))).scalars().all()
        assert rows == []


async def test_drain_marks_dead_after_max_attempts(
    patched_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After max_attempts failures, job must transition to dead — that is what
    oncall watches."""
    class ExplodingEmbedder:
        model_version = "fake:512"

        async def embed_image(self, image_url: str) -> list[float]:
            raise RuntimeError("boom")

    monkeypatch.setattr("app.bootstrap.container.embedding_service", lambda: ExplodingEmbedder())
    user_id, item_id = await _seed_item(patched_session_factory)

    async with patched_session_factory() as s:
        job, _ = await enqueue(
            s,
            job_type=JOB_TYPE,
            idempotency_key=f"embedding:{item_id}:fake:512",
            payload={
                "item_id": str(item_id),
                "image_url": "https://example.com/x.jpg",
                "model_version": "fake:512",
                "source_version": "image-v1",
            },
            user_id=user_id,
            target_id=item_id,
            max_attempts=2,
        )
        await s.commit()
        job_id = job.id

    # 1st drain: queued → running → failed → queued (attempt=1)
    await drain_embedding_jobs(ctx=None)
    # Force next_run_at into the past so the next drain re-claims it.
    async with patched_session_factory() as s:
        row = await s.get(AsyncJob, job_id)
        assert row is not None
        assert row.status == JOB_STATUS_QUEUED
        assert row.attempts == 1
        row.next_run_at = datetime.now(UTC) - timedelta(seconds=1)
        await s.commit()

    # 2nd drain: claim (attempt=2) → fail → dead (because attempts==max_attempts).
    await drain_embedding_jobs(ctx=None)

    async with patched_session_factory() as s:
        final = await s.get(AsyncJob, job_id)
        assert final is not None
        assert final.status == JOB_STATUS_DEAD
        assert final.attempts == 2


async def test_drain_skips_invalid_payload(
    patched_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    """A job with a malformed payload must not crash the drain loop."""
    async with patched_session_factory() as s:
        job, _ = await enqueue(
            s,
            job_type=JOB_TYPE,
            idempotency_key=f"embedding:bad:{uuid.uuid4()}",
            payload={"item_id": str(uuid.uuid4())},  # missing image_url + model_version
        )
        await s.commit()
        job_id = job.id

    processed = await drain_embedding_jobs(ctx=None)
    assert processed == 1

    async with patched_session_factory() as s:
        final = await s.get(AsyncJob, job_id)
        assert final is not None
        assert final.status == JOB_STATUS_QUEUED
        assert final.last_error == "missing payload fields"


def test_idempotency_key_format() -> None:
    item_id = uuid.UUID("11111111-1111-1111-1111-111111111111")
    key = embedding_worker._idempotency_key(item_id, "x" * 120, "y" * 128)
    assert key.startswith(f"embedding:{item_id}:")
    assert len(key) <= 128


def test_drain_constants_are_sane() -> None:
    # Regression guard: don't accidentally raise the per-tick budget so high
    # that one queue starves all other arq jobs.
    assert 1 <= embedding_worker.MAX_DRAIN_PER_TICK <= 100
    assert embedding_worker.JOB_TYPE == "embedding"


async def test_late_embedding_cannot_overwrite_a_new_source(
    patched_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, item_id = await _seed_item(patched_session_factory)
    observed: list[str] = []

    class StubEmbedding:
        model_version = "fake:512"

        async def embed_image(self, image_url: str) -> list[float]:
            observed.append(image_url)
            if image_url == "https://example.com/old":
                async with patched_session_factory() as s:
                    item = await s.get(WardrobeItem, item_id)
                    assert item is not None
                    item.embedding_source_version = "image-v2"
                    await enqueue_embedding(
                        s,
                        item_id=item_id,
                        image_url="https://example.com/new",
                        model_version=self.model_version,
                        source_version="image-v2",
                        user_id=user_id,
                    )
                    await s.commit()
                assert await drain_embedding_jobs(ctx=None) == 1
                return [0.0, 1.0]
            return [1.0, 0.0]

    monkeypatch.setattr("app.bootstrap.container.embedding_service", StubEmbedding)

    async with patched_session_factory() as s:
        s.add(ItemEmbedding(item_id=item_id, model_name="fake:512", source_version="image-v1", vector=[0.0, -1.0]))
        stale_job, _ = await enqueue_embedding(
            s,
            item_id=item_id,
            image_url="https://example.com/old",
            model_version="fake:512",
            source_version="image-v1",
            user_id=user_id,
        )
        await s.commit()
        claimed_job = await claim_one(s, [JOB_TYPE])
        assert claimed_job is not None

    await embedding_worker._process_one(claimed_job)
    assert observed == ["https://example.com/old", "https://example.com/new"]
    async with patched_session_factory() as s:
        rows = (await s.scalars(select(ItemEmbedding).where(ItemEmbedding.item_id == item_id))).all()
        assert [(row.source_version, row.vector) for row in rows] == [("image-v2", [1.0, 0.0])]
        final = await s.get(AsyncJob, stale_job.id)
        assert final is not None and final.result == {"outcome": "obsolete_source"}


async def test_current_embedding_only_matches_verified_source_and_model(
    patched_session_factory: async_sessionmaker[AsyncSession],
) -> None:
    _, item_id = await _seed_item(patched_session_factory)
    _, legacy_id = await _seed_item(patched_session_factory)
    async with patched_session_factory() as s:
        legacy = await s.get(WardrobeItem, legacy_id)
        assert legacy is not None
        legacy.embedding_source_version = None
        s.add_all([
            ItemEmbedding(item_id=item_id, model_name="other-model", source_version="image-v1", vector=[1.0]),
            ItemEmbedding(item_id=item_id, model_name="fake:512", source_version="image-v0", vector=[1.0]),
            ItemEmbedding(item_id=legacy_id, model_name="fake:512", vector=[1.0]),
        ])
        statement = (
            select(ItemEmbedding)
            .join(WardrobeItem, WardrobeItem.id == ItemEmbedding.item_id)
            .where(WardrobeItem.id.in_([item_id, legacy_id]), is_current_embedding("fake:512"))
        )
        assert (await s.scalars(statement)).all() == []
        row = await s.scalar(
            select(ItemEmbedding).where(ItemEmbedding.item_id == item_id, ItemEmbedding.model_name == "fake:512")
        )
        assert row is not None
        row.source_version = "image-v1"
        assert (await s.scalars(statement)).all() == [row]


async def test_outfit_confirm_enqueues_only_the_two_candidates_with_real_crops(
    db_session: AsyncSession,
) -> None:
    from app.application.import_tasks.confirm_import import ConfirmImageImportTask, ConfirmItemInput
    from app.infrastructure.persistence.models.import_task import ImportTask
    from app.infrastructure.persistence.repositories.import_task_repo import SqlAlchemyImportTaskRepository
    from app.infrastructure.persistence.repositories.wardrobe_repo import SqlAlchemyWardrobeRepository

    owner = uuid.uuid4()
    db_session.add(User(id=owner))
    await db_session.flush()
    source_key = f"users/{owner}/wardrobe/outfit.jpg"
    crop_keys = [f"users/{owner}/wardrobe/crops/{i}.jpg" for i in range(2)]
    task = ImportTask(
        user_id=owner,
        input_type="outfit_image",
        source_type="album",
        task_status="waiting_confirm",
        input_payload={
            "image_url": f"http://localhost:9000/local/files/{source_key}",
            "source_object_key": source_key,
        },
        candidate_payload={
            "candidate_items": [
                {
                    "temp_item_id": f"garment-{i}",
                    "category": category,
                    **(
                        {"crop_image_url": f"http://localhost:9000/local/files/{crop_keys[i]}",
                         "crop_object_key": crop_keys[i]}
                        if i < len(crop_keys) else {}
                    ),
                }
                for i, category in enumerate(["top", "bottom", "shoes"])
            ],
        },
    )
    db_session.add(task)
    await db_session.flush()
    inputs = [
        ConfirmItemInput(temp_item_id=f"garment-{i}", title=category, category=category, color_main="blue")
        for i, category in enumerate(["top", "bottom", "shoes"])
    ]
    use_case = ConfirmImageImportTask(
        SqlAlchemyImportTaskRepository(db_session), SqlAlchemyWardrobeRepository(db_session)
    )
    confirmed = await use_case.confirm_items(task.id, owner, inputs)
    jobs = (
        await db_session.scalars(select(AsyncJob).where(AsyncJob.user_id == owner, AsyncJob.job_type == JOB_TYPE))
    ).all()
    by_item = {job.target_id: job.payload for job in jobs}
    assert len(jobs) == 2
    for confirmed_entry, key in zip(confirmed[:2], crop_keys, strict=True):
        payload = by_item[confirmed_entry.item.id]
        assert payload is not None
        assert payload["image_key"] == key
        assert payload["image_url"] is None
        assert payload["source_version"] == confirmed_entry.item.embedding_source_version
    assert confirmed[2].item.id not in by_item
    assert confirmed[2].item.embedding_source_version is None
