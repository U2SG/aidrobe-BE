import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.jobs.recommendation_jobs import cron_departure_pre_generate, cron_nightly_fallback

logger = structlog.get_logger(__name__)


def create_scheduler(session_factory: async_sessionmaker[AsyncSession]) -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()

    async def run_departure_job() -> None:
        async with session_factory() as session:
            await cron_departure_pre_generate(session)
            await session.commit()

    async def run_nightly_job() -> None:
        async with session_factory() as session:
            await cron_nightly_fallback(session)
            await session.commit()

    scheduler.add_job(run_departure_job, "interval", minutes=5, id="departure_pre_generate")
    scheduler.add_job(run_nightly_job, "cron", hour=3, minute=0, id="nightly_fallback")
    logger.warning(
        "scheduler_single_process_required",
        message="Run APScheduler in one process only to avoid duplicate jobs.",
    )
    return scheduler
