from urllib.parse import urlparse

try:
    from arq.connections import RedisSettings
    from arq.cron import cron
except ImportError:
    RedisSettings = None
    cron = None

from app.application.fit_profile.build_fit_profile import build_all_fit_profiles
from app.config.settings import get_settings
from app.infrastructure.persistence.session import AsyncSessionLocal
from app.infrastructure.workers.cutout_worker import (
    drain_cutout_jobs,
    reconcile_dead_cutout_items,
)
from app.infrastructure.workers.embedding_worker import drain_embedding_jobs
from app.infrastructure.workers.image_worker import process_image_job
from app.infrastructure.workers.import_worker import process_import_task
from app.infrastructure.workers.job_runtime import recover_expired_leases
from app.infrastructure.workers.ootd_poster_worker import (
    drain_ootd_poster_jobs,
    reconcile_dead_ootd_poster_assets,
)
from app.infrastructure.workers.outfit_board_worker import (
    drain_outfit_board_jobs,
    reconcile_dead_outfit_board_assets,
)
from app.infrastructure.workers.pdf_worker import drain_pdf_ingest_jobs
from app.infrastructure.workers.style_learn_worker import learn_style_source
from app.infrastructure.workers.user_purge_worker import drain_user_purge_jobs


async def build_fit_profiles(ctx: object) -> int:
    async with AsyncSessionLocal() as session:
        count = await build_all_fit_profiles(session)
        await session.commit()
        return count


async def recover_async_job_leases(ctx: object) -> int:
    """Sweep all expired-lease `running` rows back to `queued` so subsequent
    drains re-claim them. Single shared sweep across all job_types — the
    `lease_expires_at` partial index makes this cheap regardless of how many
    queues we add later."""
    async with AsyncSessionLocal() as session:
        recovered = await recover_expired_leases(session)
        await session.commit()
    return recovered


def _redis_settings_from_url(url: str):  # type: ignore[no-untyped-def]
    if RedisSettings is None:
        return None
    parsed = urlparse(url)
    return RedisSettings(
        host=parsed.hostname or "localhost",
        port=parsed.port or 6379,
        database=int((parsed.path or "/0").lstrip("/") or "0"),
        password=parsed.password,
        ssl=parsed.scheme == "rediss",
    )


settings = get_settings()


class WorkerSettings:
    functions = [
        process_import_task,
        process_image_job,
        learn_style_source,
        build_fit_profiles,
        drain_embedding_jobs,
        drain_outfit_board_jobs,
        drain_ootd_poster_jobs,
        drain_cutout_jobs,
        drain_pdf_ingest_jobs,
        drain_user_purge_jobs,
        recover_async_job_leases,
        reconcile_dead_outfit_board_assets,
        reconcile_dead_ootd_poster_assets,
        reconcile_dead_cutout_items,
    ]
    # async_jobs queues drain on a fixed cadence rather than via arq enqueue —
    # producers only write the row, so cron is the trigger. Keep drain ticks
    # short for low latency, but lease recovery only every 15 min (the lease
    # default is 10 min, so 15 min recovery + 10 min lease bounds a stuck job
    # to ~25 min worst case before it re-enters the queue).
    cron_jobs = (
        [
            cron(build_fit_profiles, hour=3, minute=30, name="fit_profile_aggregation"),
            cron(drain_embedding_jobs, second={0, 30}, name="embedding_drain"),
            cron(drain_outfit_board_jobs, second={0, 30}, name="outfit_board_drain"),
            cron(drain_ootd_poster_jobs, second={0, 30}, name="ootd_poster_drain"),
            cron(drain_cutout_jobs, second={0, 30}, name="cutout_drain"),
            # PDF ingest is heavy (download + rasterize N pages) and drains one
            # at a time; a coarser cadence is plenty.
            cron(drain_pdf_ingest_jobs, second={0}, name="pdf_ingest_drain"),
            cron(
                recover_async_job_leases,
                minute={0, 15, 30, 45},
                name="async_jobs_lease_recovery",
            ),
            cron(
                reconcile_dead_outfit_board_assets,
                minute={5, 20, 35, 50},
                name="outfit_board_reconcile_dead_assets",
            ),
            cron(
                reconcile_dead_ootd_poster_assets,
                minute={6, 21, 36, 51},
                name="ootd_poster_reconcile_dead_assets",
            ),
            cron(
                reconcile_dead_cutout_items,
                minute={7, 22, 37, 52},
                name="cutout_reconcile_dead_items",
            ),
            # Purge cadence is hourly: grace boundary is days, so finer-grain
            # ticking just wastes claim_one queries.
            cron(
                drain_user_purge_jobs,
                minute={10},
                name="user_purge_drain",
            ),
        ]
        if cron is not None
        else []
    )
    redis_settings = _redis_settings_from_url(settings.ARQ_REDIS_URL)
    job_timeout = settings.ARQ_JOB_TIMEOUT_SECONDS
    max_jobs = settings.ARQ_MAX_JOBS
