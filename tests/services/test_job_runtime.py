"""Phase C1 — async_jobs runtime tests.

Cover:
- enqueue idempotency (duplicate key returns existing row, no second insert)
- claim_one with FOR UPDATE SKIP LOCKED + commit-on-claim
- mark_succeeded / mark_failed retry backoff vs. dead transition
- guarded finalization (status='running')
- lease-expiry recovery for crashed workers
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.workers.job_runtime import (
    JOB_STATUS_DEAD,
    JOB_STATUS_QUEUED,
    JOB_STATUS_RUNNING,
    JOB_STATUS_SUCCEEDED,
    _backoff_for,
    claim_one,
    enqueue,
    mark_failed,
    mark_succeeded,
    recover_expired_leases,
)


def _key() -> str:
    return f"test:{uuid.uuid4()}"


async def test_enqueue_inserts_when_key_is_new(db_session: AsyncSession) -> None:
    job, inserted = await enqueue(
        db_session,
        job_type="embedding",
        idempotency_key=_key(),
        payload={"item_id": str(uuid.uuid4())},
    )
    assert inserted is True
    assert job.status == JOB_STATUS_QUEUED
    assert job.attempts == 0
    assert job.max_attempts == 3


async def test_enqueue_is_idempotent_on_duplicate_key(db_session: AsyncSession) -> None:
    key = _key()
    first, inserted_first = await enqueue(db_session, job_type="embedding", idempotency_key=key, payload={"v": 1})
    second, inserted_second = await enqueue(db_session, job_type="embedding", idempotency_key=key, payload={"v": 2})
    assert inserted_first is True
    assert inserted_second is False
    assert first.id == second.id
    assert first.payload == {"v": 1}
    count = (
        await db_session.execute(select(func.count()).select_from(AsyncJob).where(AsyncJob.idempotency_key == key))
    ).scalar_one()
    assert count == 1


async def test_claim_one_picks_oldest_queued_and_sets_lease(db_session: AsyncSession) -> None:
    j1, _ = await enqueue(db_session, job_type="embedding", idempotency_key=_key())
    j2, _ = await enqueue(db_session, job_type="embedding", idempotency_key=_key())
    # Mark j1 as already-running so claim returns j2.
    j1.status = JOB_STATUS_RUNNING
    await db_session.commit()

    claimed = await claim_one(db_session, ["embedding"])
    assert claimed is not None
    assert claimed.id == j2.id
    assert claimed.status == JOB_STATUS_RUNNING
    assert claimed.attempts == 1
    assert claimed.lease_expires_at is not None
    assert claimed.lease_expires_at > datetime.now(UTC)


async def test_claim_one_returns_none_when_nothing_due(db_session: AsyncSession) -> None:
    job, _ = await enqueue(db_session, job_type="embedding", idempotency_key=_key())
    job.next_run_at = datetime.now(UTC) + timedelta(minutes=5)
    await db_session.commit()
    assert await claim_one(db_session, ["embedding"]) is None


async def test_claim_one_filters_by_job_type(db_session: AsyncSession) -> None:
    await enqueue(db_session, job_type="embedding", idempotency_key=_key())
    await enqueue(db_session, job_type="outfit_board", idempotency_key=_key())
    await db_session.commit()
    claimed = await claim_one(db_session, ["llm_ask"])
    assert claimed is None


async def test_concurrent_claim_one_does_not_double_pick(postgres_url: str) -> None:
    """Two concurrent transactions must NOT both claim the same job — that is
    the whole point of FOR UPDATE SKIP LOCKED."""
    engine_a = create_async_engine(postgres_url, future=True)
    engine_b = create_async_engine(postgres_url, future=True)
    seed_engine = create_async_engine(postgres_url, future=True)
    try:
        async with AsyncSession(seed_engine, expire_on_commit=False) as seed_sess:
            await enqueue(seed_sess, job_type="embedding", idempotency_key=_key())
            await seed_sess.commit()

        async def claim_with(engine: object) -> AsyncJob | None:
            async with AsyncSession(engine, expire_on_commit=False) as sess:  # type: ignore[arg-type]
                job = await claim_one(sess, ["embedding"])
                if job is not None:
                    await asyncio.sleep(0.2)
                return job

        results = await asyncio.gather(claim_with(engine_a), claim_with(engine_b))
    finally:
        await engine_a.dispose()
        await engine_b.dispose()
        await seed_engine.dispose()

    claimed = [r for r in results if r is not None]
    assert len(claimed) == 1, "FOR UPDATE SKIP LOCKED must prevent double pickup"


async def test_claim_is_durable_after_session_close(postgres_url: str) -> None:
    """Crash-safety: claim_one commits before returning, so a worker dying
    after claim must NOT roll the row back to queued."""
    seed_engine = create_async_engine(postgres_url, future=True)
    claim_engine = create_async_engine(postgres_url, future=True)
    verify_engine = create_async_engine(postgres_url, future=True)
    try:
        async with AsyncSession(seed_engine, expire_on_commit=False) as s:
            job, _ = await enqueue(s, job_type="embedding", idempotency_key=_key())
            await s.commit()
            job_id = job.id

        # Simulate a worker that claims then immediately dies (no finalize).
        async with AsyncSession(claim_engine, expire_on_commit=False) as s:
            claimed = await claim_one(s, ["embedding"])
            assert claimed is not None and claimed.id == job_id
        # Session closes here without mark_succeeded/mark_failed.

        # A fresh session must see the row STILL running with attempts=1.
        async with AsyncSession(verify_engine, expire_on_commit=False) as s:
            row = await s.get(AsyncJob, job_id)
            assert row is not None
            assert row.status == JOB_STATUS_RUNNING
            assert row.attempts == 1
            assert row.lease_expires_at is not None
    finally:
        await seed_engine.dispose()
        await claim_engine.dispose()
        await verify_engine.dispose()


async def test_mark_succeeded_records_costs(db_session: AsyncSession) -> None:
    await enqueue(db_session, job_type="llm_ask", idempotency_key=_key())
    await db_session.commit()
    claimed = await claim_one(db_session, ["llm_ask"])
    assert claimed is not None
    ok = await mark_succeeded(
        db_session,
        claimed.id,
        claimed_attempt=claimed.attempts,
        result={"answer": "ok"},
        cost_tokens=42,
        cost_cents=7,
    )
    await db_session.commit()
    assert ok is True
    row = await db_session.get(AsyncJob, claimed.id)
    assert row is not None
    assert row.status == JOB_STATUS_SUCCEEDED
    assert row.result == {"answer": "ok"}
    assert row.cost_tokens == 42
    assert row.cost_cents == 7
    assert row.last_error is None
    assert row.lease_expires_at is None


async def test_mark_succeeded_is_noop_on_non_running(db_session: AsyncSession) -> None:
    job, _ = await enqueue(db_session, job_type="llm_ask", idempotency_key=_key())
    await db_session.commit()
    # Never claimed → still queued.
    # Use any attempt — fence + status guard both fail because not running.
    ok = await mark_succeeded(db_session, job.id, claimed_attempt=1, result={"answer": "stale"})
    await db_session.commit()
    assert ok is False
    row = await db_session.get(AsyncJob, job.id)
    assert row is not None
    assert row.status == JOB_STATUS_QUEUED
    assert row.result is None


async def test_mark_failed_first_time_schedules_retry(db_session: AsyncSession) -> None:
    await enqueue(db_session, job_type="embedding", idempotency_key=_key())
    await db_session.commit()
    claimed = await claim_one(db_session, ["embedding"])
    assert claimed is not None
    new_status = await mark_failed(db_session, claimed.id, claimed_attempt=claimed.attempts, error="boom")
    await db_session.commit()
    assert new_status == JOB_STATUS_QUEUED
    row = await db_session.get(AsyncJob, claimed.id)
    assert row is not None
    assert row.status == JOB_STATUS_QUEUED
    assert row.attempts == 1
    assert row.last_error == "boom"
    assert row.lease_expires_at is None
    assert row.next_run_at > datetime.now(UTC)


async def test_mark_failed_after_max_attempts_marks_dead(db_session: AsyncSession) -> None:
    job, _ = await enqueue(db_session, job_type="embedding", idempotency_key=_key(), max_attempts=2)
    await db_session.commit()
    claimed = await claim_one(db_session, ["embedding"])
    assert claimed is not None
    assert await mark_failed(db_session, claimed.id, claimed_attempt=claimed.attempts, error="e1") == JOB_STATUS_QUEUED
    await db_session.commit()
    # Force the next_run_at into the past so we can claim again immediately.
    row = await db_session.get(AsyncJob, job.id)
    assert row is not None
    row.next_run_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    claimed2 = await claim_one(db_session, ["embedding"])
    assert claimed2 is not None
    assert await mark_failed(db_session, claimed2.id, claimed_attempt=claimed2.attempts, error="e2") == JOB_STATUS_DEAD
    await db_session.commit()
    final = await db_session.get(AsyncJob, job.id)
    assert final is not None
    assert final.status == JOB_STATUS_DEAD
    assert final.last_error == "e2"
    assert final.lease_expires_at is None


async def test_mark_failed_is_noop_on_non_running(db_session: AsyncSession) -> None:
    job, _ = await enqueue(db_session, job_type="embedding", idempotency_key=_key())
    await db_session.commit()
    # Job is queued, never claimed.
    result = await mark_failed(db_session, job.id, claimed_attempt=0, error="stale")
    await db_session.commit()
    assert result is None
    row = await db_session.get(AsyncJob, job.id)
    assert row is not None
    assert row.status == JOB_STATUS_QUEUED
    assert row.last_error is None


async def test_recover_expired_leases_returns_jobs_to_queued(db_session: AsyncSession) -> None:
    await enqueue(db_session, job_type="embedding", idempotency_key=_key())
    await db_session.commit()
    claimed = await claim_one(db_session, ["embedding"])
    assert claimed is not None
    # Force the lease into the past — simulates a crashed worker.
    row = await db_session.get(AsyncJob, claimed.id)
    assert row is not None
    row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()

    recovered = await recover_expired_leases(db_session)
    await db_session.commit()
    assert recovered == 1

    refreshed = await db_session.get(AsyncJob, claimed.id)
    assert refreshed is not None
    assert refreshed.status == JOB_STATUS_QUEUED
    assert refreshed.lease_expires_at is None
    # attempts is preserved so backoff accounting still bites.
    assert refreshed.attempts == 1
    assert refreshed.last_error == "lease_expired"


async def test_mark_succeeded_fences_against_reclaimed_job(
    db_session: AsyncSession,
) -> None:
    """Race: worker A claims (attempts=1), lease expires, recovery requeues,
    worker B claims (attempts=2). Worker A then tries to finalize — must
    return False so A's stale result/vector cannot land under B's claim."""
    job, _ = await enqueue(db_session, job_type="embedding", idempotency_key=_key())
    await db_session.commit()

    a_claim = await claim_one(db_session, ["embedding"])
    assert a_claim is not None
    a_attempt = a_claim.attempts
    assert a_attempt == 1

    # Simulate A's lease expiring + recovery requeuing the job.
    row = await db_session.get(AsyncJob, job.id)
    assert row is not None
    row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
    await db_session.commit()
    await recover_expired_leases(db_session)
    await db_session.commit()

    # B claims the requeued row — attempts now = 2.
    b_claim = await claim_one(db_session, ["embedding"])
    assert b_claim is not None
    assert b_claim.attempts == a_attempt + 1

    # A's stale finalize MUST fail the fence (claimed_attempt=1 ≠ row.attempts=2).
    ok = await mark_succeeded(db_session, job.id, claimed_attempt=a_attempt, result={"stale": True})
    await db_session.commit()
    assert ok is False
    final = await db_session.get(AsyncJob, job.id)
    assert final is not None
    assert final.status == JOB_STATUS_RUNNING  # still B's claim
    assert final.result is None  # A's stale write rejected
    # B can still finalize successfully on its own claim.
    ok_b = await mark_succeeded(db_session, job.id, claimed_attempt=b_claim.attempts, result={"fresh": True})
    await db_session.commit()
    assert ok_b is True
    final = await db_session.get(AsyncJob, job.id)
    assert final is not None
    assert final.status == JOB_STATUS_SUCCEEDED
    assert final.result == {"fresh": True}


async def test_recover_expired_leases_dead_letters_when_attempts_exhausted(
    db_session: AsyncSession,
) -> None:
    """A worker that keeps crashing and letting its lease expire must
    eventually hit dead-letter — otherwise oncall never gets paged."""
    job, _ = await enqueue(
        db_session,
        job_type="embedding",
        idempotency_key=_key(),
        max_attempts=2,
    )
    await db_session.commit()

    # Simulate two crash-loops: claim → expire lease → recover.
    for expected_attempts in (1, 2):
        claimed = await claim_one(db_session, ["embedding"])
        assert claimed is not None
        assert claimed.attempts == expected_attempts
        # Crash before finalize: just expire the lease in place.
        row = await db_session.get(AsyncJob, claimed.id)
        assert row is not None
        row.lease_expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await db_session.commit()
        await recover_expired_leases(db_session)
        await db_session.commit()

    final = await db_session.get(AsyncJob, job.id)
    assert final is not None
    assert final.status == JOB_STATUS_DEAD
    assert final.attempts == 2
    assert final.last_error == "lease_expired"


async def test_recover_expired_leases_skips_unexpired(db_session: AsyncSession) -> None:
    await enqueue(db_session, job_type="embedding", idempotency_key=_key())
    await db_session.commit()
    claimed = await claim_one(db_session, ["embedding"])
    assert claimed is not None
    # Lease is fresh (DEFAULT_LEASE_SECONDS into future).
    recovered = await recover_expired_leases(db_session)
    await db_session.commit()
    assert recovered == 0
    row = await db_session.get(AsyncJob, claimed.id)
    assert row is not None
    assert row.status == JOB_STATUS_RUNNING


def test_backoff_schedule_grows_then_caps() -> None:
    assert _backoff_for(0) == 60
    assert _backoff_for(1) == 60
    assert _backoff_for(2) == 300
    assert _backoff_for(3) == 1800
    assert _backoff_for(4) == 7200
    assert _backoff_for(99) == 7200


@pytest.mark.parametrize("max_attempts", [0, -1])
async def test_enqueue_rejects_invalid_max_attempts(db_session: AsyncSession, max_attempts: int) -> None:
    with pytest.raises(ValueError):
        await enqueue(
            db_session,
            job_type="embedding",
            idempotency_key=_key(),
            max_attempts=max_attempts,
        )
