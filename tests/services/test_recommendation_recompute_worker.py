from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.recommendations.background import schedule_today_recompute
from app.application.recommendations.engine import EngineItem, EngineResult, RecommendationEngine
from app.application.recommendations.first_outfit_gate import FirstOutfitGate
from app.application.recommendations.generator import RecommendationGenerator
from app.common.enums import Category, ItemStatus
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.user import User, UserSettings
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)
from app.infrastructure.workers import recommendation_recompute_worker
from app.infrastructure.workers.recommendation_recompute_worker import (
    drain_recommendation_recompute_jobs,
)


@pytest_asyncio.fixture
async def patched_session_factory(
    postgres_url: str, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_async_engine(postgres_url, future=True)
    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine, expire_on_commit=False
    )
    monkeypatch.setattr(
        recommendation_recompute_worker, "AsyncSessionLocal", factory
    )
    try:
        yield factory
    finally:
        await engine.dispose()


async def test_worker_recomputes_today_and_finalizes_job(
    patched_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixed_now = datetime(2026, 5, 1, 8, 0, tzinfo=UTC)
    monkeypatch.setattr(
        "app.application.recommendations.background.utc_now", lambda: fixed_now
    )
    invalidated: list[uuid.UUID] = []

    async def fake_invalidate(_redis, user_id: uuid.UUID) -> None:
        invalidated.append(user_id)

    monkeypatch.setattr(
        recommendation_recompute_worker,
        "invalidate_daily_recommendation_cache",
        fake_invalidate,
    )

    user_id = uuid.uuid4()
    async with patched_session_factory() as session:
        session.add(User(id=user_id))
        session.add(UserSettings(user_id=user_id, timezone="UTC"))
        for title, category, color in [
            ("White shirt", Category.TOP.value, "white"),
            ("Blue shirt", Category.TOP.value, "blue"),
            ("Black pants", Category.BOTTOM.value, "black"),
        ]:
            session.add(
                WardrobeItem(
                    user_id=user_id,
                    title=title,
                    category=category,
                    color_main=color,
                    status=ItemStatus.WEARABLE.value,
                )
            )
        await session.flush()
        job_inserted = await schedule_today_recompute(
            user_id, SqlAlchemyRecommendationRepository(session)
        )
        job = await session.scalar(
            select(AsyncJob).where(
                AsyncJob.user_id == user_id,
                AsyncJob.job_type == "recommendation_recompute",
            )
        )
        assert job is not None
        job_id = job.id
        await session.commit()

    assert job_inserted is True
    assert await drain_recommendation_recompute_jobs(ctx=None) == 1

    async with patched_session_factory() as session:
        finalized = await session.get(AsyncJob, job_id)
        daily = await SqlAlchemyRecommendationRepository(session).get_daily(
            user_id, date(2026, 5, 1)
        )
        assert finalized is not None
        assert finalized.status == "succeeded"
        assert finalized.result == {"recomputed": True}
        assert daily is not None
        assert daily.status == "ready"
        assert daily.generation_source == "read_time"
    assert invalidated == [user_id]

    # Terminal jobs do not suppress later wardrobe mutations.
    async with patched_session_factory() as session:
        inserted_after_success = await schedule_today_recompute(
            user_id, SqlAlchemyRecommendationRepository(session)
        )
        await session.commit()
    assert inserted_after_success is True


@pytest.mark.parametrize("newer_source", ["first_outfit_gate", "refresh"])
async def test_background_snapshot_cannot_overwrite_newer_first_outfit_or_refresh(
    patched_session_factory: async_sessionmaker[AsyncSession],
    monkeypatch: pytest.MonkeyPatch,
    newer_source: str,
) -> None:
    user_id = uuid.uuid4()
    now = datetime.now(UTC)
    target_date = now.date()
    monkeypatch.setattr("app.application.recommendations.background.utc_now", lambda: now)
    # Disable provider calls while retaining the recommendation worker's real
    # claim, repository, generator, and commit behavior.
    monkeypatch.setattr("app.application.recommendations.background.llm_service", lambda: None)
    monkeypatch.setattr("app.application.recommendations.background.weather_service", lambda: None)

    async with patched_session_factory() as session:
        session.add(User(id=user_id))
        await session.flush()
        session.add(UserSettings(user_id=user_id, timezone="UTC"))
        session.add_all(
            [
                WardrobeItem(
                    user_id=user_id,
                    title=category,
                    category=category,
                    color_main="black",
                    status=ItemStatus.WEARABLE.value,
                    cover_image_url="https://example.com/item.jpg",
                )
                for category in ("top", "bottom")
            ]
        )
        await session.flush()
        await schedule_today_recompute(user_id, SqlAlchemyRecommendationRepository(session))
        await session.commit()

    original_generate = RecommendationEngine.generate
    snapshot_read = asyncio.Event()
    release_stale_worker = asyncio.Event()

    async def pause_stale_generation(
        engine: RecommendationEngine, items: list[EngineItem], **kwargs
    ) -> EngineResult:
        result = await original_generate(engine, items, **kwargs)
        if not any(item.category == "shoes" for item in items):
            assert result.status == "insufficient_items"
            snapshot_read.set()
            await release_stale_worker.wait()
        return result

    monkeypatch.setattr(RecommendationEngine, "generate", pause_stale_generation)

    async with asyncio.timeout(10), asyncio.TaskGroup() as group:
        worker = group.create_task(drain_recommendation_recompute_jobs(ctx=None))
        await snapshot_read.wait()
        async with patched_session_factory() as session:
            session.add(
                WardrobeItem(
                    user_id=user_id,
                    title="shoes",
                    category="shoes",
                    color_main="white",
                    status=ItemStatus.WEARABLE.value,
                    cover_image_url="https://example.com/shoes.jpg",
                )
            )
            await session.commit()

        async with patched_session_factory() as session:
            repository = SqlAlchemyRecommendationRepository(session)
            if newer_source == "first_outfit_gate":
                gate = await FirstOutfitGate(repository).execute(user_id, target_date)
                assert gate.state == "single_outfit"
                assert gate.first_outfit is not None
            else:
                await RecommendationGenerator(repository).generate_for_user(
                    user_id, target_date, generation_source="refresh", refresh=True
                )
                gate = await FirstOutfitGate(repository).execute(user_id, target_date)
                assert not gate.generated
            fresh = await repository.get_daily(user_id, target_date)
            assert fresh is not None
            fresh_id = fresh.id
            outfit_ids = {outfit.id for outfit in fresh.outfits}
            assert outfit_ids
            await session.commit()
        release_stale_worker.set()

    assert worker.result() == 1
    async with patched_session_factory() as session:
        repository = SqlAlchemyRecommendationRepository(session)
        after = await repository.get_daily(user_id, target_date)
        assert after is not None
        assert after.id == fresh_id
        assert after.generation_source == newer_source
        assert after.status == "ready"
        assert {outfit.id for outfit in after.outfits} == outfit_ids
        assert await repository.count_current_dailies(user_id, target_date) == 1
