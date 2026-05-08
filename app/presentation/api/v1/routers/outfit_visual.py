"""Outfit visual assets router — M10."""

import uuid
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.infrastructure.persistence.session import get_db_session as get_db
from app.presentation.api.v1.deps import get_current_user

router = APIRouter(prefix="/v1", tags=["outfit-visual"])


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
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
) -> OutfitVisualAssetResponse:
    from app.infrastructure.persistence.repositories.outfit_visual_repo import (
        SqlAlchemyOutfitVisualRepository,
    )

    repo = SqlAlchemyOutfitVisualRepository(db)
    asset = await repo.get_by_id(asset_id)
    if asset is None or asset.user_id != current_user.id:
        raise FriendlyException(error_codes.NOT_FOUND, "Asset not found", status_code=404)

    return OutfitVisualAssetResponse(
        asset_id=asset.id,
        generation_status=asset.generation_status,
        # Phase E5 — prefer object_key + sign at request time so private
        # buckets work without a long-TTL URL baked into the row. Fall
        # back to the persisted image_url for legacy / non-OSS deploys.
        image_url=_resolve_render_url(asset.object_key, asset.image_url),
        visual_type=asset.visual_type,
        error_code=asset.error_code,
        error_message=asset.error_message,
    )


def _resolve_render_url(object_key: str | None, fallback_url: str | None) -> str | None:
    """Sign and return a short-lived URL when an object_key is present;
    otherwise return the persisted URL as-is. Used by both endpoints
    below so the policy is consistent."""
    if object_key:
        from app.bootstrap.container import object_storage

        try:
            return object_storage().sign_render_url(object_key)
        except Exception:
            # If signing fails (storage misconfigured), fall through to
            # the persisted URL rather than 500-ing the read.
            pass
    return fallback_url


@router.get(
    "/wardrobe-items/{item_id}/images",
    response_model=WardrobeItemImagesResponse,
)
async def get_item_images(
    item_id: uuid.UUID,
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
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
