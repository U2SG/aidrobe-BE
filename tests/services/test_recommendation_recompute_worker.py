from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.application.recommendations.background import schedule_today_recompute
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
