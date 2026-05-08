"""Per-user-per-day cost guard — Phase E3.

Aggregates `async_jobs.cost_*` per `(user_id, job_type, day)` and refuses
new work once the user's daily limit is hit. Limits are per-job-type so a
user can still do recommendations even after blowing their LLM budget.

Wiring contract:
- Use cases that enqueue cost-bearing async jobs call `assert_within_quota`
  BEFORE `enqueue`. If the quota is exhausted, FriendlyException(429) is
  raised and no job is created.
- Embedding currently passes cost_tokens=0 (local CLIP has no token bill);
  remote embedding / LLM / OCR / try-on populate the field, which is what
  this aggregation reads back.
- ENABLE_COST_GUARD off ⇒ check is a no-op. Tokens are STILL recorded on
  async_jobs so flipping the flag on later doesn't require backfill.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, time, timedelta

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.async_job import AsyncJob

logger = structlog.get_logger(__name__)


def _today_window() -> tuple[datetime, datetime]:
    now = datetime.now(UTC)
    start = datetime.combine(now.date(), time.min, tzinfo=UTC)
    return start, start + timedelta(days=1)


def _limit_for(settings: Settings, job_type: str) -> int | None:
    """Return the daily token / call limit for `job_type`, or None for
    job types that aren't gated. `None` means "no limit", not "limit=0"."""
    return {
        "llm_ask": settings.LLM_DAILY_TOKEN_LIMIT,
        "ocr_receipt": settings.OCR_DAILY_CALL_LIMIT,
        "tryon": settings.TRYON_DAILY_CALL_LIMIT,
    }.get(job_type)


async def usage_today(
    session: AsyncSession,
    user_id: uuid.UUID,
    job_type: str,
) -> int:
    """How much the user has consumed of the given job_type today.

    Token-based quotas (llm_ask) sum cost_tokens; call-count quotas
    (ocr_receipt, tryon) count rows. Anything else returns 0 — the caller
    should check `_limit_for` first."""
    start, end = _today_window()
    base = select(AsyncJob).where(
        AsyncJob.user_id == user_id,
        AsyncJob.job_type == job_type,
        AsyncJob.created_at >= start,
        AsyncJob.created_at < end,
        # Don't count dead/failed jobs — they didn't actually consume the
        # quota; only succeeded + currently-running do.
        AsyncJob.status.in_(("succeeded", "running")),
    )
    if job_type == "llm_ask":
        return int(
            (
                await session.execute(
                    select(func.coalesce(func.sum(AsyncJob.cost_tokens), 0)).select_from(
                        base.subquery()
                    )
                )
            ).scalar()
            or 0
        )
    return int(
        (
            await session.execute(
                select(func.count()).select_from(base.subquery())
            )
        ).scalar()
        or 0
    )


async def assert_within_quota(
    session: AsyncSession,
    user_id: uuid.UUID,
    job_type: str,
    *,
    cost_tokens: int = 1,
) -> None:
    """Raise FriendlyException(429) when consuming `cost_tokens` for
    `job_type` today would exceed the user's daily limit.

    `cost_tokens` is the projected cost of the operation about to run; for
    call-count quotas it's ignored (each call counts as 1). When
    ENABLE_COST_GUARD is False this is a no-op — the function still runs
    for parity but does not raise."""
    settings = get_settings()
    if not settings.ENABLE_COST_GUARD:
        return
    limit = _limit_for(settings, job_type)
    if limit is None:
        return
    used = await usage_today(session, user_id, job_type)
    projected = used + (cost_tokens if job_type == "llm_ask" else 1)
    if projected > limit:
        logger.warning(
            "cost_guard.exhausted",
            user_id=str(user_id),
            job_type=job_type,
            used=used,
            projected=projected,
            limit=limit,
        )
        raise FriendlyException(
            error_codes.STATE_CONFLICT,
            f"daily {job_type} quota exhausted",
            status_code=429,
        )


async def cost_summary_today(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> dict[str, dict[str, int]]:
    """Build the payload behind GET /v1/user/cost-summary. Includes one
    entry per gated job_type even when usage is zero, so the client can
    render a stable list."""
    settings = get_settings()
    out: dict[str, dict[str, int]] = {}
    for job_type in ("llm_ask", "ocr_receipt", "tryon"):
        limit = _limit_for(settings, job_type)
        used = await usage_today(session, user_id, job_type)
        out[job_type] = {
            "used": used,
            "limit": limit if limit is not None else 0,
            "remaining": max(0, (limit or 0) - used),
        }
    return out
