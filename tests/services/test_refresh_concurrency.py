"""Phase D3 — concurrent refresh advisory-lock serialization.

Asserts that pg_advisory_xact_lock in archive_current_and_revise prevents
two parallel `refresh` requests on the same (user_id, date) from both
INSERTing rev=N+1 and crashing on the partial-unique index.

Two engines so each call truly runs in its own transaction context (the
shared `db_session` fixture would serialize via session.begin under the
hood and miss the race we're testing)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import date

from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.common.enums import Category, ItemStatus
from app.infrastructure.persistence.models.recommendation import DailyRecommendation
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem


async def _seed_user_with_first_revision(
    engine: object, *, has_existing_current: bool
) -> uuid.UUID:
    """Plant a user (and optionally one current daily) so we can exercise
    both the "no current row exists" race (fixed by advisory lock) and
    the "current row exists" race (covered by the previous SELECT FOR
    UPDATE fix) under the same harness."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,  # type: ignore[arg-type]
        expire_on_commit=False,
    )
    user_id = uuid.uuid4()
    async with factory() as s:
        s.add(User(id=user_id))
        await s.flush()  # FK-safe ordering for the children below
        for title, category, color in [
            ("White shirt", Category.TOP.value, "white"),
            ("Black pants", Category.BOTTOM.value, "black"),
        ]:
            s.add(
                WardrobeItem(
                    user_id=user_id,
                    title=title,
                    category=category,
                    color_main=color,
                    status=ItemStatus.WEARABLE.value,
                )
            )
        if has_existing_current:
            s.add(
                DailyRecommendation(
                    user_id=user_id,
                    recommendation_date=date.today(),
                    status="ready",
                    generation_source="manual",
                    revision=1,
                    is_current=True,
                )
            )
        await s.commit()
    return user_id


async def _archive_in_isolated_session(engine: object, user_id: uuid.UUID) -> int:
    """Run archive_current_and_revise in its own session/transaction and
    return the revision the caller got. Sleeps briefly mid-transaction
    so the racing call has a chance to interleave."""
    from sqlalchemy.ext.asyncio import async_sessionmaker

    from app.infrastructure.persistence.repositories.recommendation_repo import (
        SqlAlchemyRecommendationRepository,
    )

    factory: async_sessionmaker[AsyncSession] = async_sessionmaker(
        engine,  # type: ignore[arg-type]
        expire_on_commit=False,
    )
    async with factory() as s:
        repo = SqlAlchemyRecommendationRepository(s)
        new = DailyRecommendation(
            user_id=user_id,
            recommendation_date=date.today(),
            status="ready",
            generation_source="refresh",
            context_json=None,
        )
        result = await repo.archive_current_and_revise(new)
        # Hold the transaction briefly so the other coroutine has time
        # to hit the advisory lock and queue behind us.
        await asyncio.sleep(0.2)
        await s.commit()
        return result.revision


async def test_concurrent_refresh_serializes_via_advisory_lock_first_revision(
    postgres_url: str,
) -> None:
    """The interesting race: NO existing current row. The previous
    SELECT FOR UPDATE fix only locked existing rows, so two concurrent
    first-refreshes both saw max_rev=0 and INSERT rev=1 → IntegrityError.
    Advisory lock must serialize regardless of row existence."""
    seed_engine = create_async_engine(postgres_url, future=True)
    engine_a = create_async_engine(postgres_url, future=True)
    engine_b = create_async_engine(postgres_url, future=True)
    try:
        user_id = await _seed_user_with_first_revision(
            seed_engine, has_existing_current=False
        )
        results = await asyncio.gather(
            _archive_in_isolated_session(engine_a, user_id),
            _archive_in_isolated_session(engine_b, user_id),
            return_exceptions=True,
        )
    finally:
        await seed_engine.dispose()
        await engine_a.dispose()
        await engine_b.dispose()

    # Neither call should have raised — advisory lock serialized them.
    for r in results:
        assert not isinstance(r, BaseException), f"unexpected error: {r!r}"
    revisions = sorted(results)  # type: ignore[type-var]
    assert revisions == [1, 2], f"expected sequential revisions 1,2; got {revisions}"


async def test_concurrent_refresh_serializes_with_existing_current_row(
    postgres_url: str,
) -> None:
    """Sanity check the advisory lock also covers the "row exists" path
    (the case the prior SELECT FOR UPDATE was supposed to handle)."""
    seed_engine = create_async_engine(postgres_url, future=True)
    engine_a = create_async_engine(postgres_url, future=True)
    engine_b = create_async_engine(postgres_url, future=True)
    try:
        user_id = await _seed_user_with_first_revision(
            seed_engine, has_existing_current=True
        )
        results = await asyncio.gather(
            _archive_in_isolated_session(engine_a, user_id),
            _archive_in_isolated_session(engine_b, user_id),
            return_exceptions=True,
        )
    finally:
        await seed_engine.dispose()
        await engine_a.dispose()
        await engine_b.dispose()

    for r in results:
        assert not isinstance(r, BaseException), f"unexpected error: {r!r}"
    # rev=1 was the seeded current; refreshes archive it then INSERT 2 + 3.
    revisions = sorted(results)  # type: ignore[type-var]
    assert revisions == [2, 3], f"expected sequential revisions 2,3; got {revisions}"
