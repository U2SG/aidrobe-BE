import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import structlog
from sqlalchemy import select, text

from app.application.recommendations.generator import RecommendationGenerator
from app.bootstrap.container import llm_service, weather_service
from app.common.enums import RecommendationGenerationSource
from app.common.local_time import local_datetime, utc_now
from app.config.settings import get_settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)
from app.infrastructure.persistence.repositories.style_repo import SqlAlchemyStyleRepository
from app.infrastructure.workers.job_runtime import enqueue

logger = structlog.get_logger(__name__)

RECOMMENDATION_RECOMPUTE_JOB_TYPE = "recommendation_recompute"


async def schedule_today_recompute(
    user_id: uuid.UUID,
    repository: SqlAlchemyRecommendationRepository | None = None,
    style_repository: SqlAlchemyStyleRepository | None = None,
    *,
    generation_source: RecommendationGenerationSource = RecommendationGenerationSource.READ_TIME,
) -> bool:
    if repository is None:
        logger.warning(
            "recommendation_recompute_skipped",
            reason="missing_repository",
            user_id=str(user_id),
        )
        return False
    now = utc_now()
    timezone = await repository.get_user_timezone(user_id)
    target_date = local_datetime(
        now, timezone, get_settings().DEFAULT_TIMEZONE
    ).date()
    # Serialize producer-side coalescing per user. Only queued work is merged:
    # if a job is already running, a later mutation needs one queued successor
    # because it may have arrived after the worker loaded its wardrobe snapshot.
    lock_key = int.from_bytes(
        hashlib.sha1(f"recompute-enqueue:{user_id}".encode()).digest()[:8],
        "big",
        signed=True,
    )
    await repository.session.execute(
        text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key}
    )
    active = await repository.session.scalar(
        select(AsyncJob)
        .where(
            AsyncJob.user_id == user_id,
            AsyncJob.job_type == RECOMMENDATION_RECOMPUTE_JOB_TYPE,
            AsyncJob.status == "queued",
        )
        .order_by(AsyncJob.created_at.desc())
        .limit(1)
    )
    if active is not None:
        active.payload = {
            **(active.payload or {}),
            "target_date": target_date.isoformat(),
            "requested_at": now.isoformat(),
            "generation_source": generation_source.value,
        }
        await repository.session.flush()
        _log_recompute_schedule(active, False)
        return False

    job, inserted = await enqueue(
        repository.session,
        job_type=RECOMMENDATION_RECOMPUTE_JOB_TYPE,
        idempotency_key=f"recommendation_recompute:{user_id}:{uuid.uuid4().hex}",
        payload={
            "user_id": str(user_id),
            "target_date": target_date.isoformat(),
            "requested_at": now.isoformat(),
            "generation_source": generation_source.value,
        },
        user_id=user_id,
    )
    _log_recompute_schedule(job, inserted)
    return inserted


def _log_recompute_schedule(job: AsyncJob, inserted: bool) -> None:
    logger.info(
        "recommendation_recompute_scheduled",
        job_id=str(job.id),
        user_id=str(job.user_id),
        inserted=inserted,
    )


class RecommendationBackgroundService:
    def __init__(
        self,
        repository: SqlAlchemyRecommendationRepository,
        style_repository: SqlAlchemyStyleRepository | None = None,
    ) -> None:
        self.repository = repository
        self.style_repository = style_repository

    async def recompute_today(
        self,
        user_id: uuid.UUID,
        force: bool = False,
        requested_at: datetime | None = None,
        generation_source: RecommendationGenerationSource = RecommendationGenerationSource.READ_TIME,
    ) -> bool:
        now = utc_now()
        timezone = await self.repository.get_user_timezone(user_id)
        today = local_datetime(now, timezone, get_settings().DEFAULT_TIMEZONE).date()
        existing = await self.repository.get_daily(user_id, today)
        if (
            existing
            and existing.generation_source == "refresh"
            and existing.updated_at
            and requested_at is not None
        ):
            updated_at = existing.updated_at
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=UTC)
            if updated_at >= requested_at:
                logger.info(
                    "recommendation_recompute_skipped",
                    reason="superseded_by_newer_daily",
                    user_id=str(user_id),
                    recommendation_id=str(existing.id),
                    revision=existing.revision,
                )
                return False
        if existing and await self.repository.daily_has_engagement(existing.id):
            logger.info(
                "recommendation_recompute_skipped",
                reason="daily_has_engagement",
                user_id=str(user_id),
                recommendation_id=str(existing.id),
                revision=existing.revision,
            )
            return False
        if not force and existing and existing.updated_at:
            updated_at = existing.updated_at
            if updated_at.tzinfo is None:
                updated_at = updated_at.replace(tzinfo=UTC)
            if now - updated_at < timedelta(seconds=30):
                return False
        await RecommendationGenerator(
            self.repository,
            self.style_repository,
            llm=llm_service(),
            weather_service=weather_service(),
        ).generate_for_user(
            user_id,
            today,
            generation_source.value,
            preserve_refresh_after=requested_at,
        )
        return True

    async def users_without_departure_time(self) -> list[uuid.UUID]:
        return [user.id for user in await self.repository.users_without_departure_time()]

    async def due_departure_users(self, now: datetime) -> list[uuid.UUID]:
        users = await self.repository.users_due_for_departure_generation(
            now, get_settings().DEFAULT_TIMEZONE
        )
        return [user.id for user in users]
