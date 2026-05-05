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
from app.infrastructure.workers.image_worker import process_image_job
from app.infrastructure.workers.import_worker import process_import_task
from app.infrastructure.workers.style_learn_worker import learn_style_source


async def build_fit_profiles(ctx: object) -> int:
    async with AsyncSessionLocal() as session:
        count = await build_all_fit_profiles(session)
        await session.commit()
        return count


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
    ]
    cron_jobs = (
        [cron(build_fit_profiles, hour=3, minute=30, name="fit_profile_aggregation")]
        if cron is not None
        else []
    )
    redis_settings = _redis_settings_from_url(settings.ARQ_REDIS_URL)
    job_timeout = settings.ARQ_JOB_TIMEOUT_SECONDS
    max_jobs = settings.ARQ_MAX_JOBS
