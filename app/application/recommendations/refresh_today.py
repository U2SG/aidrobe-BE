"""Phase D3 — user-initiated /today/refresh.

Distinct from background recompute (`background.py`): refresh is intentional,
ignores engagement, and writes a new revision so prior wear/feedback remain
attached to the OLD outfit_ids. Rate-limited so a misbehaving client cannot
burn LLM/embedding cost.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

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


async def refresh_today_recommendation(
    user_id: uuid.UUID,
    repository: SqlAlchemyRecommendationRepository,
    style_repository: SqlAlchemyStyleRepository | None = None,
    *,
    scene: str | None = None,
    context_constraints: list[str] | None = None,
) -> DailyRecommendation:
    """Generate a new revision for today.

    Raises FriendlyException(429) when the per-user-per-day refresh count
    has been hit. Rate-limit count includes the original revision-1 row,
    so a limit of N means N total daily rows (1 background + N-1 refresh).

    Feature-flagged: when `ENABLE_TODAY_REFRESH=False` the endpoint should
    not have called us (router gate). This belt-and-suspenders raises 503
    so a misconfigured router can't accidentally bypass the gate."""
    settings = get_settings()
    if not settings.ENABLE_TODAY_REFRESH:
        # Plan: feature flag is operator-controlled; failing closed here is
        # safer than silently doing the work because routes occasionally
        # forget to gate.
        raise FriendlyException(
            error_codes.FORBIDDEN,
            "today refresh is not enabled",
            status_code=403,
        )

    today = datetime.now(UTC).date()
    used = await repository.count_revisions_for_date(user_id, today)
    # <= 0 means "no daily limit" (testing-phase escape hatch); any positive
    # value caps total daily revisions.
    limit = settings.TODAY_REFRESH_DAILY_LIMIT
    if limit > 0 and used >= limit:
        logger.warning(
            "today_refresh_rate_limited",
            user_id=str(user_id),
            target_date=str(today),
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
        target_date=str(today),
        used=used,
        limit=limit,
    )
    return await RecommendationGenerator(
        repository,
        style_repository,
        llm=llm_service(),
        weather_service=weather_service(),
    ).generate_for_user(
        user_id,
        today,
        generation_source="refresh",
        scene=scene,
        refresh=True,
        context_constraints=context_constraints,
    )
