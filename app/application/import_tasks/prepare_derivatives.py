"""Queue derived work using the verified photo of a confirmed garment."""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.cost_guard import resolve_quota_limit
from app.application.paid_generation_admission import REASON_DAILY_BUDGET, attach, release, reserve
from app.common.enums import CutoutStatus
from app.common.exceptions import FriendlyException
from app.config.settings import get_settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.workers.cutout_worker import MAX_ATTEMPTS, cutout_job_key, enqueue_item_cutout
from app.infrastructure.workers.embedding_worker import enqueue_embedding


async def prepare_item_derivatives(
    session: AsyncSession,
    item: WardrobeItem,
    *,
    source_key: str | None,
    source_url: str | None,
    source_version: str,
) -> None:
    """Publish the current true image and enqueue both consumers in this transaction.

    The caller holds the item lock (or is creating it). Only a single-garment
    original or a trustworthy garment crop belongs here.
    """
    from app.bootstrap.container import embedding_model_version

    if item.deleted_at is not None or not (source_key or source_url):
        return
    if source_key and source_url:
        source_url = source_url.split("?", 1)[0].split("#", 1)[0]
    item.embedding_source_version = source_version
    await enqueue_embedding(
        session,
        item_id=item.id,
        image_url=source_url,
        image_key=source_key,
        source_version=source_version,
        model_version=embedding_model_version(),
        user_id=item.user_id,
    )

    if not get_settings().ENABLE_ITEM_CUTOUT or item.cutout_status == CutoutStatus.READY.value:
        return
    key = cutout_job_key(item.id)
    if await session.scalar(select(AsyncJob.id).where(AsyncJob.idempotency_key == key)) is not None:
        return
    try:
        admission = await reserve(
            session,
            user_id=item.user_id,
            job_type="cutout",
            reservation_key=key,
            attempt_grant=MAX_ATTEMPTS,
            limit=resolve_quota_limit("cutout"),
        )
    except FriendlyException as exc:
        if exc.status_code != 429 or (exc.data or {}).get("reason") != REASON_DAILY_BUDGET:
            raise
        return

    job, created = await enqueue_item_cutout(
        session,
        item_id=item.id,
        user_id=item.user_id,
        source_url=source_url,
        source_key=source_key,
        source_version=source_version,
        expected_cover_url=item.cover_image_url,
        category=item.category,
        title=item.title,
        color=item.color_main,
    )
    if created:
        await attach(session, admission.reservation_id, job_id=job.id, target_id=None)
        item.cutout_status = CutoutStatus.QUEUED.value
    elif admission.created:
        await release(session, admission.reservation_id, reason="job_already_queued")
