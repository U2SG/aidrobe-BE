"""Enqueue and collect storage object keys for PIPL deletion compliance.

Called from two sites:
- `clear_all_app_data` (profile router): collect BEFORE clearing rows, enqueue AFTER.
- `user_purge_worker`: collect BEFORE deleting the User row, enqueue in same session.

Each enqueue uses a unique idempotency key so repeated clear-data invocations each
get their own job — unlike user_purge (one-per-user) this must run every time.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.image_job import ImageProcessingJob
from app.infrastructure.persistence.models.outfit_visual import OutfitVisualAsset
from app.infrastructure.persistence.models.purchase_decision import PurchaseCandidate
from app.infrastructure.persistence.models.recommendation import WearHistory
from app.infrastructure.persistence.models.shopping import ShoppingSenseCheck
from app.infrastructure.persistence.models.style import UserStyleProfile
from app.infrastructure.persistence.models.wardrobe import WardrobeItem, WardrobeItemImage
from app.infrastructure.storage.object_key import extract_internal_object_key
from app.infrastructure.workers.job_runtime import enqueue

logger = structlog.get_logger(__name__)

JOB_TYPE = "storage_purge"


def _url_key(url: str | None, settings: Settings) -> str | None:
    if not url:
        return None
    return extract_internal_object_key(url, settings)


async def collect_user_object_keys(
    session: AsyncSession,
    user_id: uuid.UUID,
    settings: Settings,
) -> list[str]:
    """Return all internal storage object keys owned by this user.

    URL columns are resolved via `extract_internal_object_key` — external
    CDN / third-party URLs yield None and are silently skipped.
    `object_key` columns are taken directly when non-null.
    """
    keys: list[str] = []

    # WardrobeItem — URL columns only; no object_key column on the item itself.
    rows = (
        await session.execute(
            select(WardrobeItem.cover_image_url, WardrobeItem.original_image_url).where(
                WardrobeItem.user_id == user_id
            )
        )
    ).all()
    for cover, original in rows:
        for k in (_url_key(cover, settings), _url_key(original, settings)):
            if k:
                keys.append(k)

    # WardrobeItemImage — has both object_key and image_url; prefer object_key.
    img_rows = (
        await session.execute(
            select(WardrobeItemImage.object_key, WardrobeItemImage.image_url).where(
                WardrobeItemImage.item_id.in_(
                    select(WardrobeItem.id).where(WardrobeItem.user_id == user_id)
                )
            )
        )
    ).all()
    for obj_key, img_url in img_rows:
        if obj_key:
            keys.append(obj_key)
        elif (k := _url_key(img_url, settings)):
            keys.append(k)

    # OutfitVisualAsset
    ova_rows = (
        await session.execute(
            select(OutfitVisualAsset.object_key, OutfitVisualAsset.image_url).where(
                OutfitVisualAsset.user_id == user_id
            )
        )
    ).all()
    for obj_key, img_url in ova_rows:
        if obj_key:
            keys.append(obj_key)
        elif (k := _url_key(img_url, settings)):
            keys.append(k)

    # ShoppingSenseCheck
    ssc_rows = (
        await session.execute(
            select(ShoppingSenseCheck.object_key, ShoppingSenseCheck.image_url).where(
                ShoppingSenseCheck.user_id == user_id
            )
        )
    ).all()
    for obj_key, img_url in ssc_rows:
        if obj_key:
            keys.append(obj_key)
        elif (k := _url_key(img_url, settings)):
            keys.append(k)

    # PurchaseCandidate (V3-004) — a screenshot / order image is stored as its
    # stable object key with no URL column, so the key is taken directly. Row
    # deletion cascades via the FK to users.id, but the blob behind the key
    # would be orphaned in object storage without this.
    pc_rows = (
        await session.execute(
            select(PurchaseCandidate.source_object_key).where(
                PurchaseCandidate.user_id == user_id,
                PurchaseCandidate.source_object_key.is_not(None),
            )
        )
    ).all()
    for (obj_key,) in pc_rows:
        if obj_key:
            keys.append(obj_key)

    # WearHistory
    wh_rows = (
        await session.execute(
            select(WearHistory.ootd_image_url).where(WearHistory.user_id == user_id)
        )
    ).all()
    for (url,) in wh_rows:
        if (k := _url_key(url, settings)):
            keys.append(k)

    # ImageProcessingJob
    ipj_rows = (
        await session.execute(
            select(ImageProcessingJob.source_image_url).where(
                ImageProcessingJob.user_id == user_id
            )
        )
    ).all()
    for (url,) in ipj_rows:
        if (k := _url_key(url, settings)):
            keys.append(k)

    # UserStyleProfile — image_url (inspiration images) + source_url (PDF and
    # other learned-source files that also land in internal storage).
    usp_rows = (
        await session.execute(
            select(UserStyleProfile.image_url, UserStyleProfile.source_url).where(
                UserStyleProfile.user_id == user_id
            )
        )
    ).all()
    for img_url, src_url in usp_rows:
        for k in (_url_key(img_url, settings), _url_key(src_url, settings)):
            if k:
                keys.append(k)

    # Data-export bundles — the worker records the bundle's object key in the
    # job result. Collected here so clear-data (which does NOT prefix-sweep,
    # the account stays active) still deletes the pre-clear export bundle.
    export_rows = (
        await session.execute(
            select(AsyncJob.result).where(
                AsyncJob.user_id == user_id, AsyncJob.job_type == "user_export"
            )
        )
    ).all()
    for (result,) in export_rows:
        key = (result or {}).get("object_key")
        if key:
            keys.append(key)

    # Deduplicate while preserving order.
    seen: set[str] = set()
    unique: list[str] = []
    for k in keys:
        if k not in seen:
            seen.add(k)
            unique.append(k)

    logger.info(
        "storage_purge.keys_collected",
        user_id=str(user_id),
        count=len(unique),
    )
    return unique


async def enqueue_storage_purge(
    session: AsyncSession,
    user_id: uuid.UUID,
    keys: Sequence[str],
    correlation_id: str | None = None,
    *,
    sweep_prefix: bool = False,
) -> None:
    """Enqueue a storage_purge job. Uses a unique idempotency key so multiple
    enqueues (e.g. clear-data → purge) each produce their own job.

    `sweep_prefix=True` additionally deletes EVERYTHING under `users/<id>/` —
    only safe for hard account purge, where the account is gone and nothing
    new can be uploaded. Clear-data must keep it False: the account stays
    active and the worker may run up to an hour later, so a sweep would eat
    uploads made after the clear request. When False, deletion is bounded to
    the keys collected at enqueue time."""
    if not keys and not sweep_prefix:
        logger.info("storage_purge.skipped_no_keys", user_id=str(user_id))
        return
    # Unique key per invocation — storage purge can fire more than once per
    # user (clear-data is repeatable), so we must NOT reuse a per-user key.
    idempotency_key = f"storage_purge:{user_id}:{uuid.uuid4()}"
    await enqueue(
        session,
        job_type=JOB_TYPE,
        idempotency_key=idempotency_key,
        payload={
            "user_id": str(user_id),
            "keys": list(keys),
            "sweep_prefix": sweep_prefix,
        },
        user_id=user_id,
        target_id=user_id,
        correlation_id=correlation_id,
    )
