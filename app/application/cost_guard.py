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

Concurrency model:
- The DB-aggregate path is read-after-write and TOCTOU-leaky: two parallel
  requests each see used=900 (limit 1000), each gets +200 budget approved,
  total spend = 1300. For LLM with single-request cost variance up to
  thousands of tokens this is a real over-spend hazard.
- Redis INCRBY (when configured) provides atomic check-and-increment.
  The DB aggregate stays the audit ground truth; Redis is a per-day
  spending counter that drains via TTL=24h.
- When Redis is unavailable (test env, infra outage), we fall back to
  the DB aggregate path. Better to soft-limit + log than refuse all
  cost-bearing requests.
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
        "ootd_poster": settings.OOTD_POSTER_DAILY_CALL_LIMIT,
        "cutout": settings.ITEM_CUTOUT_DAILY_LIMIT,
    }.get(job_type)


async def usage_today(
    session: AsyncSession,
    user_id: uuid.UUID,
    job_type: str,
) -> int:
    """How much the user has consumed of the given job_type today.

    Two quota modes with different counting semantics:

    - **Token mode** (`llm_ask`): SUM(cost_tokens) over status IN
      (succeeded, running, failed, dead). Provider partial completions
      are real billed cost even when the wrapper job fails / dead-letters,
      so ignoring failed/dead would underbill the user.

    - **Call-count mode** (`ocr_receipt`, `tryon`): COUNT(*) over status
      IN (succeeded, running). Counting failed retries here would shrink
      a 100-call quota to ~33 if every call retries 3 times before
      succeeding, which would be a hostile regression for users with
      flaky network conditions. Only completed work consumes the quota.
    """
    start, end = _today_window()

    if job_type == "llm_ask":
        # Token quotas count ALL cost-bearing statuses including failed/
        # dead — see docstring rationale.
        statuses: tuple[str, ...] = ("succeeded", "running", "failed", "dead")
        return int(
            (
                await session.execute(
                    select(func.coalesce(func.sum(AsyncJob.cost_tokens), 0)).where(
                        AsyncJob.user_id == user_id,
                        AsyncJob.job_type == job_type,
                        AsyncJob.created_at >= start,
                        AsyncJob.created_at < end,
                        AsyncJob.status.in_(statuses),
                    )
                )
            ).scalar()
            or 0
        )

    # Call-count quotas: only completed work counts. A retry that
    # eventually succeeds counts once (the succeeded row); failed/dead
    # retries don't burn quota.
    statuses = ("succeeded", "running")
    return int(
        (
            await session.execute(
                select(func.count()).select_from(AsyncJob).where(
                    AsyncJob.user_id == user_id,
                    AsyncJob.job_type == job_type,
                    AsyncJob.created_at >= start,
                    AsyncJob.created_at < end,
                    AsyncJob.status.in_(statuses),
                )
            )
        ).scalar()
        or 0
    )


def _redis_quota_key(user_id: uuid.UUID, job_type: str) -> str:
    start, _ = _today_window()
    return f"cost_guard:{user_id}:{job_type}:{start.strftime('%Y%m%d')}"


async def assert_within_quota(
    session: AsyncSession,
    user_id: uuid.UUID,
    job_type: str,
    *,
    cost_tokens: int = 1,
    redis_client: object | None = None,
) -> None:
    """Raise FriendlyException(429) when consuming `cost_tokens` for
    `job_type` today would exceed the user's daily limit.

    `cost_tokens` is the projected cost of the operation about to run; for
    call-count quotas it's ignored (each call counts as 1). When
    ENABLE_COST_GUARD is False this is a no-op — the function still runs
    for parity but does not raise.

    `redis_client` (when provided) is used for atomic INCRBY → race-safe
    check-and-reserve. When omitted we fall back to the DB aggregate
    path, which is TOCTOU-leaky but acceptable for low concurrency."""
    settings = get_settings()
    if not settings.ENABLE_COST_GUARD:
        return
    limit = _limit_for(settings, job_type)
    if limit is None:
        return

    delta = cost_tokens if job_type == "llm_ask" else 1

    if redis_client is not None:
        # Atomic path. INCRBY returns the post-increment value; if that
        # exceeds limit we DECRBY back so a denied request doesn't
        # poison the counter for subsequent requests within budget.
        try:
            key = _redis_quota_key(user_id, job_type)
            new_total = int(await redis_client.incrby(key, delta))  # type: ignore[attr-defined]
            # Set TTL on first write only (best-effort; INCRBY on a
            # non-existent key starts at 0 + delta, no TTL set).
            await redis_client.expire(key, 86400)  # type: ignore[attr-defined]
            if new_total > limit:
                # Roll back our reservation so other in-budget requests
                # don't false-fail behind us.
                await redis_client.incrby(key, -delta)  # type: ignore[attr-defined]
                logger.warning(
                    "cost_guard.exhausted",
                    user_id=str(user_id),
                    job_type=job_type,
                    used=new_total - delta,
                    projected=new_total,
                    limit=limit,
                    path="redis",
                )
                raise FriendlyException(
                    error_codes.STATE_CONFLICT,
                    f"daily {job_type} quota exhausted",
                    status_code=429,
                )
            return
        except FriendlyException:
            raise
        except Exception as exc:
            # Redis path failed; fall through to DB aggregate. Log but
            # don't fail the request — better soft-limit than block.
            logger.warning("cost_guard.redis_unavailable", error=str(exc))

    # DB-aggregate fallback (TOCTOU-leaky; documented).
    used = await usage_today(session, user_id, job_type)
    projected = used + delta
    if projected > limit:
        logger.warning(
            "cost_guard.exhausted",
            user_id=str(user_id),
            job_type=job_type,
            used=used,
            projected=projected,
            limit=limit,
            path="db",
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
    for job_type in ("llm_ask", "ocr_receipt", "tryon", "ootd_poster", "cutout"):
        limit = _limit_for(settings, job_type)
        used = await usage_today(session, user_id, job_type)
        out[job_type] = {
            "used": used,
            "limit": limit if limit is not None else 0,
            "remaining": max(0, (limit or 0) - used),
        }
    return out
