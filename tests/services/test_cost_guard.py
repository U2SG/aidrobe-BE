"""Phase E3 — cost guard quota + cost-summary aggregation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.cost_guard import (
    assert_within_quota,
    cost_summary_today,
    usage_today,
)
from app.common.exceptions import FriendlyException
from app.config.settings import Settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.user import User


@pytest.fixture(autouse=True)
def _enable_guard(monkeypatch: pytest.MonkeyPatch) -> None:
    """Default-on for these tests so the no-op short-circuit doesn't hide
    quota logic. Limits stay small to make the math easy to read."""

    def _settings() -> Settings:
        return Settings(
            APP_ENV="dev",
            ENABLE_COST_GUARD=True,
            LLM_DAILY_TOKEN_LIMIT=1000,
            OCR_DAILY_CALL_LIMIT=2,
            TRYON_DAILY_CALL_LIMIT=1,
        )

    monkeypatch.setattr("app.application.cost_guard.get_settings", _settings)


async def _seed_user(session: AsyncSession) -> uuid.UUID:
    user_id = uuid.uuid4()
    session.add(User(id=user_id))
    await session.flush()
    return user_id


async def _add_succeeded_job(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    job_type: str,
    cost_tokens: int = 0,
    days_ago: int = 0,
) -> None:
    """Insert a synthetic completed job. `days_ago>0` puts it outside the
    today-window so we can prove the aggregation actually filters by day."""
    when = datetime.now(UTC) - timedelta(days=days_ago)
    session.add(
        AsyncJob(
            id=uuid.uuid4(),
            job_type=job_type,
            idempotency_key=f"test:{uuid.uuid4()}",
            user_id=user_id,
            status="succeeded",
            attempts=1,
            max_attempts=3,
            cost_tokens=cost_tokens,
            cost_cents=0,
            payload={},
            created_at=when,
            updated_at=when,
            next_run_at=when,
        )
    )
    await session.flush()


async def test_usage_today_counts_only_today(db_session: AsyncSession) -> None:
    user_id = await _seed_user(db_session)
    await _add_succeeded_job(db_session, user_id, job_type="llm_ask", cost_tokens=300)
    await _add_succeeded_job(
        db_session, user_id, job_type="llm_ask", cost_tokens=999, days_ago=1
    )
    assert await usage_today(db_session, user_id, "llm_ask") == 300


async def test_usage_today_includes_failed_and_dead_jobs(
    db_session: AsyncSession,
) -> None:
    """LLM provider partial completions / OCR billed-on-attempt are real
    incurred costs even on failed/dead jobs. The quota must include them
    so a flaky retry loop doesn't silently inflate the user's bill."""
    user_id = await _seed_user(db_session)
    await _add_succeeded_job(db_session, user_id, job_type="llm_ask", cost_tokens=200)

    when = datetime.now(UTC)
    for status in ("failed", "dead"):
        db_session.add(
            AsyncJob(
                id=uuid.uuid4(),
                job_type="llm_ask",
                idempotency_key=f"test:{uuid.uuid4()}",
                user_id=user_id,
                status=status,
                attempts=3,
                max_attempts=3,
                cost_tokens=100,
                cost_cents=0,
                created_at=when,
                updated_at=when,
                next_run_at=when,
            )
        )
    await db_session.flush()
    # 200 (succeeded) + 100 (failed) + 100 (dead) = 400.
    assert await usage_today(db_session, user_id, "llm_ask") == 400


async def test_call_count_quota_for_ocr(db_session: AsyncSession) -> None:
    user_id = await _seed_user(db_session)
    await _add_succeeded_job(db_session, user_id, job_type="ocr_receipt")
    await _add_succeeded_job(db_session, user_id, job_type="ocr_receipt")
    assert await usage_today(db_session, user_id, "ocr_receipt") == 2


async def test_assert_within_quota_passes_under_limit(
    db_session: AsyncSession,
) -> None:
    user_id = await _seed_user(db_session)
    await _add_succeeded_job(db_session, user_id, job_type="llm_ask", cost_tokens=900)
    # Limit is 1000; one more 50-token call is fine.
    await assert_within_quota(db_session, user_id, "llm_ask", cost_tokens=50)


async def test_assert_within_quota_raises_on_overrun(
    db_session: AsyncSession,
) -> None:
    user_id = await _seed_user(db_session)
    await _add_succeeded_job(db_session, user_id, job_type="llm_ask", cost_tokens=900)
    with pytest.raises(FriendlyException) as exc_info:
        await assert_within_quota(db_session, user_id, "llm_ask", cost_tokens=200)
    assert exc_info.value.status_code == 429


async def test_assert_within_quota_call_count_overrun(
    db_session: AsyncSession,
) -> None:
    user_id = await _seed_user(db_session)
    await _add_succeeded_job(db_session, user_id, job_type="ocr_receipt")
    await _add_succeeded_job(db_session, user_id, job_type="ocr_receipt")
    with pytest.raises(FriendlyException):
        await assert_within_quota(db_session, user_id, "ocr_receipt")


async def test_assert_within_quota_noop_when_flag_off(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        "app.application.cost_guard.get_settings",
        lambda: Settings(APP_ENV="dev", ENABLE_COST_GUARD=False),
    )
    user_id = await _seed_user(db_session)
    # Pre-fill way over the (would-be) limit; check returns silently.
    await _add_succeeded_job(db_session, user_id, job_type="llm_ask", cost_tokens=999_999)
    await assert_within_quota(db_session, user_id, "llm_ask", cost_tokens=999_999)


async def test_assert_within_quota_ignores_non_gated_job_types(
    db_session: AsyncSession,
) -> None:
    """`embedding` doesn't have a per-user limit (cost is local CLUI compute,
    not tokens). The guard should pass it through silently."""
    user_id = await _seed_user(db_session)
    await assert_within_quota(db_session, user_id, "embedding", cost_tokens=999_999)


async def test_cost_summary_returns_all_gated_types(db_session: AsyncSession) -> None:
    user_id = await _seed_user(db_session)
    await _add_succeeded_job(db_session, user_id, job_type="llm_ask", cost_tokens=400)
    summary = await cost_summary_today(db_session, user_id)
    assert summary["llm_ask"] == {"used": 400, "limit": 1000, "remaining": 600}
    # Untouched job_types render as empty.
    assert summary["ocr_receipt"]["used"] == 0
    assert summary["tryon"]["used"] == 0
    assert summary["tryon"]["remaining"] == 1
