"""Phase D3 — user-initiated /today/refresh.

Distinct from background recompute (`background.py`): refresh is intentional,
ignores engagement, and writes a new revision so prior wear/feedback remain
attached to the OLD outfit_ids. Rate-limited so a misbehaving client cannot
burn LLM/embedding cost.
"""

from __future__ import annotations

import uuid
from datetime import date

import structlog

from app.application.recommendations.generator import RecommendationGenerator
from app.bootstrap.container import llm_service, weather_service
from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.config.settings import get_settings
from app.infrastructure.persistence.models.recommendation import DailyRecommendation
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)
from app.infrastructure.persistence.repositories.style_repo import SqlAlchemyStyleRepository

logger = structlog.get_logger(__name__)


async def assert_refresh_allowed(
    repository: SqlAlchemyRecommendationRepository,
    user_id: uuid.UUID,
    target_date: date,
) -> None:
    """Cheap eligibility gate: feature flag + per-day refresh limit. Call this
    BEFORE any expensive work (e.g. an LLM context-override parse) so an
    over-quota client can't burn that spend just to receive a 429."""
    settings = get_settings()
    if not settings.ENABLE_TODAY_REFRESH:
        raise FriendlyException(
            error_codes.FORBIDDEN,
            "today refresh is not enabled",
            status_code=403,
        )
    used = await repository.count_revisions_for_date(user_id, target_date)
    # <= 0 means "no daily limit" (testing-phase escape hatch); any positive
    # value caps total daily revisions.
    limit = settings.TODAY_REFRESH_DAILY_LIMIT
    if limit > 0 and used >= limit:
        logger.warning(
            "today_refresh_rate_limited",
            user_id=str(user_id),
            target_date=str(target_date),
            used=used,
            limit=limit,
        )
        raise FriendlyException(
            error_codes.STATE_CONFLICT,
            "daily refresh limit reached",
            status_code=429,
        )
    logger.info(
        "today_refresh_started",
        user_id=str(user_id),
        target_date=str(target_date),
        used=used,
        limit=limit,
    )


async def refresh_today_recommendation(
    user_id: uuid.UUID,
    repository: SqlAlchemyRecommendationRepository,
    style_repository: SqlAlchemyStyleRepository | None = None,
    *,
    target_date: date,
    scene: str | None = None,
    context_constraints: list[str] | None = None,
) -> DailyRecommendation:
    """Generate a new revision for today.

    Raises FriendlyException(429) when the per-user-per-day refresh count
    has been hit. Rate-limit count includes the original revision-1 row,
    so a limit of N means N total daily rows (1 background + N-1 refresh).

    Feature-flagged: when `ENABLE_TODAY_REFRESH=False` the endpoint should
    not have called us (router gate). This belt-and-suspenders raises 403
    so a misconfigured router can't accidentally bypass the gate."""
    # Serialize the final quota check with every write for this user/date. The
    # router's earlier check is only a cheap fast-fail; without this lock two
    # concurrent requests can both pass the count and both spend on generation.
    await repository.lock_daily_write(user_id, target_date)
    await assert_refresh_allowed(repository, user_id, target_date)
    return await RecommendationGenerator(
        repository,
        style_repository,
        llm=llm_service(),
        weather_service=weather_service(),
    ).generate_for_user(
        user_id,
        target_date,
        generation_source="refresh",
        scene=scene,
        refresh=True,
        context_constraints=context_constraints,
    )
