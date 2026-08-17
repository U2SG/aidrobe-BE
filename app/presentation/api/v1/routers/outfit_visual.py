"""Outfit visual assets router — M10."""

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.infrastructure.persistence.session import get_db_session as get_db
from app.presentation.api.v1.deps import get_current_user
from app.presentation.api.v1.render_url import resolve_render_url as _resolve_render_url

router = APIRouter(prefix="/v1", tags=["outfit-visual"])

# A job that has been queued this long with no worker claims it is effectively
# stale — surface 'failed' so the frontend stops polling instead of looping
# until the user navigates away.
_STALE_QUEUED_MINUTES = 15


class OutfitVisualAssetResponse(BaseModel):
    asset_id: uuid.UUID
    generation_status: str
    image_url: str | None = None
    visual_type: str | None = None
    error_code: str | None = None
    error_message: str | None = None

    model_config = {"from_attributes": True}


class WardrobeItemImagesResponse(BaseModel):
    item_id: uuid.UUID
    images: list[dict[str, Any]]


@router.get(
    "/outfit-visual-assets/{asset_id}",
    response_model=OutfitVisualAssetResponse,
)
async def get_outfit_visual_asset(
    asset_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[Any, Depends(get_current_user)],
) -> OutfitVisualAssetResponse:
    from app.infrastructure.persistence.repositories.outfit_visual_repo import (
        SqlAlchemyOutfitVisualRepository,
    )

    repo = SqlAlchemyOutfitVisualRepository(db)
    asset = await repo.get_by_id(asset_id)
    if asset is None or asset.user_id != current_user.id:
        raise FriendlyException(error_codes.NOT_FOUND, "Asset not found", status_code=404)

    generation_status = asset.generation_status
    error_code = asset.error_code

    # `quality_check` is V3-018's §6.2 intermediate state: non-terminal, so a
    # worker that dead-letters mid-gate must surface `failed` here exactly as
    # it does from `processing` — a poller stops only on terminal states.
    if generation_status in ("queued", "processing", "quality_check"):
        generation_status, error_code = await _resolve_pending_status(
            db, asset_id, generation_status, error_code, asset.created_at
        )

    return OutfitVisualAssetResponse(
        asset_id=asset.id,
        generation_status=generation_status,
        # Phase E5 — prefer object_key + sign at request time so private
        # buckets work without a long-TTL URL baked into the row. Fall
        # back to the persisted image_url for legacy / non-OSS deploys.
        image_url=_resolve_render_url(asset.object_key, asset.image_url),
        visual_type=asset.visual_type,
        error_code=error_code,
        error_message=asset.error_message,
    )


async def _resolve_pending_status(
    db: AsyncSession,
    asset_id: uuid.UUID,
    current_status: str,
    current_error_code: str | None,
    created_at: datetime,
) -> tuple[str, str | None]:
    """Return (generation_status, error_code) for a queued/processing asset.

    Surfaces 'failed' immediately when:
    - The backing async_job is dead (worker exhausted all retries), OR
    - The asset has been queued for >15 min with no worker claiming it
      (dev environment without arq running).

    This bypasses the APScheduler-based reconciler so the frontend receives
    a terminal state without waiting for the scheduler cron."""
    from sqlalchemy import select

    from app.infrastructure.persistence.models.async_job import AsyncJob

    has_dead_job = await db.scalar(
        select(AsyncJob.id)
        .where(AsyncJob.target_id == asset_id, AsyncJob.status == "dead")
        .limit(1)
    )
    if has_dead_job:
        return "failed", current_error_code or "generation_failed"

    if current_status == "queued" and datetime.now(UTC) - created_at > timedelta(
        minutes=_STALE_QUEUED_MINUTES
    ):
        return "failed", "worker_unavailable"

    return current_status, current_error_code


@router.get(
    "/wardrobe-items/{item_id}/images",
    response_model=WardrobeItemImagesResponse,
)
async def get_item_images(
    item_id: uuid.UUID,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[Any, Depends(get_current_user)],
) -> WardrobeItemImagesResponse:
    from sqlalchemy import select

    from app.infrastructure.persistence.models.wardrobe import WardrobeItem, WardrobeItemImage

    result = await db.execute(
        select(WardrobeItem).where(
            WardrobeItem.id == item_id,
            WardrobeItem.user_id == current_user.id,
            WardrobeItem.deleted_at.is_(None),
        )
    )
    item = result.scalar_one_or_none()
    if item is None:
        raise FriendlyException(error_codes.NOT_FOUND, "Item not found", status_code=404)

    img_result = await db.execute(
        select(WardrobeItemImage).where(WardrobeItemImage.item_id == item_id)
    )
    images = [
        {
            "image_type": img.image_type,
            "image_url": _resolve_render_url(img.object_key, img.image_url),
        }
        for img in img_result.scalars().all()
    ]

    return WardrobeItemImagesResponse(item_id=item_id, images=images)
