import asyncio

import structlog
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config.settings import get_settings
from app.infrastructure.logging.setup import setup_logging
from app.infrastructure.persistence.session import AsyncSessionLocal
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


async def run_scheduler_forever() -> None:
    settings = get_settings()
    setup_logging(settings.LOG_LEVEL, settings.LOG_FORMAT)
    scheduler = create_scheduler(AsyncSessionLocal)
    scheduler.start()
    logger.info("scheduler_started")
    try:
        await asyncio.Event().wait()
    finally:
        scheduler.shutdown()


if __name__ == "__main__":
    asyncio.run(run_scheduler_forever())
