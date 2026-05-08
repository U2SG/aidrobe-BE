import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.persistence.models.outfit_visual import OutfitVisualAsset


class SqlAlchemyOutfitVisualRepository:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def add(self, asset: OutfitVisualAsset) -> OutfitVisualAsset:
        self._session.add(asset)
        await self._session.flush()
        return asset

    async def get_by_id(self, asset_id: uuid.UUID) -> OutfitVisualAsset | None:
        result = await self._session.execute(
            select(OutfitVisualAsset).where(OutfitVisualAsset.id == asset_id)
        )
        return result.scalar_one_or_none()

    async def get_by_outfit_id(self, outfit_id: uuid.UUID) -> list[OutfitVisualAsset]:
        result = await self._session.execute(
            select(OutfitVisualAsset).where(OutfitVisualAsset.outfit_id == outfit_id)
        )
        return list(result.scalars().all())

    async def update_status(
        self,
        asset_id: uuid.UUID,
        status: str,
        image_url: str | None = None,
        error_code: str | None = None,
        error: str | None = None,
        object_key: str | None = None,
    ) -> None:
        asset = await self.get_by_id(asset_id)
        if asset is None:
            return
        asset.generation_status = status
        if image_url is not None:
            asset.image_url = image_url
        if object_key is not None:
            asset.object_key = object_key
        if error_code is not None:
            asset.error_code = error_code
        if error is not None:
            asset.error_message = error
        await self._session.flush()
