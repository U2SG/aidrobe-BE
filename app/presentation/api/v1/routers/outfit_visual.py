"""Outfit visual assets router — M10."""

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.presentation.api.v1.deps import get_current_user
from app.infrastructure.persistence.session import get_db_session as get_db

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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Asset not found")

    return OutfitVisualAssetResponse(
        asset_id=asset.id,
        generation_status=asset.generation_status,
        image_url=asset.image_url,
        visual_type=asset.visual_type,
        error_code=asset.error_code,
        error_message=asset.error_message,
    )


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
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Item not found")

    img_result = await db.execute(
        select(WardrobeItemImage).where(WardrobeItemImage.item_id == item_id)
    )
    images = [
        {"image_type": img.image_type, "image_url": img.image_url}
        for img in img_result.scalars().all()
    ]

    return WardrobeItemImagesResponse(item_id=item_id, images=images)
