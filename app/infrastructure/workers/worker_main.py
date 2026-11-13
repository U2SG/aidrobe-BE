from urllib.parse import urlparse

try:
    from arq.connections import RedisSettings
    from arq.cron import cron
except ImportError:  # arq is optional at runtime
    RedisSettings = None  # type: ignore[assignment, misc]
    cron = None  # type: ignore[assignment]

from app.application.fit_profile.build_fit_profile import build_all_fit_profiles
from app.bootstrap.analytics import analytics_worker_shutdown, analytics_worker_startup
from app.config.settings import get_settings
from app.infrastructure.persistence.session import AsyncSessionLocal
from app.infrastructure.workers.avatar_candidate_worker import drain_avatar_candidate_jobs
from app.infrastructure.workers.avatar_preparation_worker import drain_avatar_preparation_jobs
from app.infrastructure.workers.avatar_purge_worker import drain_avatar_purge_jobs
from app.infrastructure.workers.cutout_worker import (
    drain_cutout_jobs,
    reconcile_dead_cutout_items,
)
from app.infrastructure.workers.embedding_worker import drain_embedding_jobs
from app.infrastructure.workers.image_worker import process_image_job
from app.infrastructure.workers.import_worker import (
    drain_import_jobs,
    process_import_task,
    reconcile_dead_import_tasks,
)
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
from app.infrastructure.workers.recommendation_recompute_worker import (
    drain_recommendation_recompute_jobs,
)
from app.infrastructure.workers.static_tryon_worker import (
    drain_static_tryon_jobs,
    reconcile_dead_static_tryon_assets,
    reconcile_static_tryon_reservations,
)
from app.infrastructure.workers.storage_purge_worker import drain_storage_purge_jobs
from app.infrastructure.workers.style_learn_worker import learn_style_source
from app.infrastructure.workers.user_export_worker import drain_user_export_jobs
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
    on_startup = analytics_worker_startup
    on_shutdown = analytics_worker_shutdown
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
        drain_recommendation_recompute_jobs,
        drain_user_export_jobs,
        drain_user_purge_jobs,
        drain_storage_purge_jobs,
        drain_avatar_candidate_jobs,
        drain_avatar_preparation_jobs,
        drain_avatar_purge_jobs,
        drain_static_tryon_jobs,
        drain_import_jobs,
        recover_async_job_leases,
        reconcile_dead_outfit_board_assets,
        reconcile_dead_ootd_poster_assets,
        reconcile_dead_static_tryon_assets,
        reconcile_static_tryon_reservations,
        reconcile_dead_cutout_items,
        reconcile_dead_import_tasks,
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
            # Import is a user-visible "recognizing" state, so drain often to
            # trim queue-wait before the ~seconds Vision/OCR call.
            cron(drain_import_jobs, second=set(range(0, 60, 5)), name="import_drain"),
            # Tight cadence (every 5s): the cutout is a user-visible "polishing"
            # state, so trim queue-wait before the ~30s gpt-image-2 gen. claim_one
            # is a cheap FOR UPDATE SKIP LOCKED query, so polling often is fine.
            cron(drain_cutout_jobs, second=set(range(0, 60, 5)), name="cutout_drain"),
            # PDF ingest is heavy (download + rasterize N pages) and drains one
            # at a time; a coarser cadence is plenty.
            cron(drain_pdf_ingest_jobs, second={0}, name="pdf_ingest_drain"),
            # V3-012 avatar candidate detection. Nothing user-visible waits
            # on it (the whole point is that it rides behind a flow that has
            # already answered), so a once-a-minute cadence is right: local
            # segmentation is CPU-heavy and there is no queue-wait to trim.
            cron(drain_avatar_candidate_jobs, second={15}, name="avatar_candidate_drain"),
            # V3-013 avatar preparation. A user IS waiting on this one — the
            # profile sits in `preparing` until it lands — so it drains every 5s
            # like the other user-visible generators, not on the candidate
            # detector's once-a-minute cadence.
            cron(
                drain_avatar_preparation_jobs,
                second=set(range(0, 60, 5)),
                name="avatar_preparation_drain",
            ),
            # V3-013 avatar object purge. The rows are already gone and the user
            # has their answer, so latency does not matter; what matters is that
            # it runs, since a dropped purge leaves person material in the
            # bucket after a deletion request.
            cron(drain_avatar_purge_jobs, second={45}, name="avatar_purge_drain"),
            # V3-017 static try-on preview. A user is watching a spinner, so
            # this drains on the 5s user-visible-generator cadence rather than
            # the background one — same reasoning as avatar preparation.
            cron(
                drain_static_tryon_jobs,
                second=set(range(0, 60, 5)),
                name="static_tryon_drain",
            ),
            cron(
                drain_recommendation_recompute_jobs,
                second=set(range(0, 60, 5)),
                name="recommendation_recompute_drain",
            ),
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
            # V3-017. Offset from its siblings for the same reason they are
            # offset from each other: four reconcilers firing on the same
            # minute would contend for the same `async_jobs` scan.
            cron(
                reconcile_dead_static_tryon_assets,
                minute={8, 23, 38, 53},
                name="static_tryon_reconcile_dead_assets",
            ),
            cron(
                reconcile_dead_import_tasks,
                minute={8, 23, 38, 53},
                name="import_reconcile_dead_tasks",
            ),
            # V3-026. The drain already settles what it finished, so this is the
            # backstop for the case the drain cannot cover: a worker that died
            # mid-attempt and whose job was dead-lettered by lease recovery, not
            # by a drain tick. Offset by a minute from the dead-asset sweep so
            # the two do not scan `async_jobs` together, and scheduled after it
            # so a reservation settles against an asset state that has already
            # converged.
            cron(
                reconcile_static_tryon_reservations,
                minute={9, 24, 39, 54},
                name="static_tryon_reconcile_reservations",
            ),
            # Purge cadence is hourly: grace boundary is days, so finer-grain
            # ticking just wastes claim_one queries.
            cron(
                drain_user_purge_jobs,
                minute={10},
                name="user_purge_drain",
            ),
            # Storage purge: same hourly cadence as user_purge; offset by 1 min
            # to avoid competing on claim_one at the same second.
            cron(
                drain_storage_purge_jobs,
                minute={11},
                name="storage_purge_drain",
            ),
            # Export is a user-visible "being prepared" polling state — drain
            # every 30s (offset from the other :0/:30 drains).
            cron(
                drain_user_export_jobs,
                second={10, 40},
                name="user_export_drain",
            ),
        ]
        if cron is not None
        else []
    )
    redis_settings = _redis_settings_from_url(settings.ARQ_REDIS_URL)
    job_timeout = settings.ARQ_JOB_TIMEOUT_SECONDS
    max_jobs = settings.ARQ_MAX_JOBS
    # Heartbeat cadence for the compose healthcheck (`arq --check`): arq writes
    # its health-check key with TTL = interval + 1s, so the arq default (3600s)
    # would take up to an hour to flag a hung worker. 60s bounds detection to
    # ~3 min with the compose retries.
    health_check_interval = 60
