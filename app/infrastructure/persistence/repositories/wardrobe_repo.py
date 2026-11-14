import uuid
from collections import defaultdict
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import ImageType
from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.wardrobe import WardrobeItem, WardrobeItemImage
from app.infrastructure.storage.object_key import names_internal_storage, trusted_item_image_key

ImageSource = tuple[str | None, str | None]


def _image_source(
    images: list[WardrobeItemImage], url: str | None, preferred_types: tuple[str, ...]
) -> ImageSource:
    if url:
        match = next((image for image in images if image.image_url == url and image.object_key), None)
        return (match.object_key if match else None, url)
    for image_type in preferred_types:
        image = next((image for image in images if image.image_type == image_type), None)
        if image is not None:
            return image.object_key, image.image_url
    return None, None


def _checked_source(source: ImageSource, owner_id: uuid.UUID, settings: Settings) -> ImageSource:
    key, url = source
    if url and names_internal_storage(url, settings):
        verified_key = trusted_item_image_key(url, owner_id, settings)
        if verified_key is None or (key is not None and key != verified_key):
            return None, None
        return verified_key, None
    if key:
        return key, None
    return None, url


async def load_wardrobe_image_sources(
    session: AsyncSession, items: Sequence[WardrobeItem]
) -> dict[uuid.UUID, tuple[ImageSource, ImageSource]]:
    """Resolve saved cover/original references for already-authorized wardrobe rows."""
    if not items:
        return {}
    rows = await session.scalars(
        select(WardrobeItemImage)
        .where(WardrobeItemImage.item_id.in_([item.id for item in items]))
        .order_by(WardrobeItemImage.created_at.desc(), WardrobeItemImage.id.desc())
    )
    images_by_item: dict[uuid.UUID, list[WardrobeItemImage]] = defaultdict(list)
    for row in rows:
        images_by_item[row.item_id].append(row)

    settings = get_settings()
    sources = {}
    for item in items:
        images = images_by_item[item.id]
        original = _image_source(images, item.original_image_url, (ImageType.ORIGINAL.value,))
        cover = _image_source(
            images, item.cover_image_url, (ImageType.STANDARD.value, ImageType.CROP.value, ImageType.ORIGINAL.value)
        )
        original = _checked_source(original, item.user_id, settings)
        cover = _checked_source(cover, item.user_id, settings)
        sources[item.id] = (cover if cover != (None, None) else original, original)
    return sources


class SqlAlchemyWardrobeRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, item: WardrobeItem) -> WardrobeItem:
        self.session.add(item)
        await self.session.flush()
        return item

    async def add_image(self, image: WardrobeItemImage) -> WardrobeItemImage:
        self.session.add(image)
        await self.session.flush()
        return image

    async def get_for_user(self, item_id: uuid.UUID, user_id: uuid.UUID) -> WardrobeItem | None:
        return cast(
            WardrobeItem | None,
            await self.session.scalar(
            select(WardrobeItem).where(
                WardrobeItem.id == item_id,
                WardrobeItem.user_id == user_id,
                WardrobeItem.deleted_at.is_(None),
            )
            ),
        )

    async def list_for_user(
        self,
        user_id: uuid.UUID,
        category: str | None = None,
        status: str | None = None,
        season: str | None = None,
        color: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[WardrobeItem], int]:
        filters = [WardrobeItem.user_id == user_id, WardrobeItem.deleted_at.is_(None)]
        if category is not None:
            filters.append(WardrobeItem.category == category)
        if status is not None:
            filters.append(WardrobeItem.status == status)
        if season is not None:
            # season_tags JSONB shape: {"tags": ["spring", ...]} (V3 M14)
            # Navigate to the tags key first, then use @> array containment.
            # An all-season garment belongs in every season's list.
            filters.append(
                or_(
                    WardrobeItem.season_tags["tags"].contains([season]),
                    WardrobeItem.season_tags["tags"].contains(["all_season"]),
                )
            )
        if color is not None:
            filters.append(WardrobeItem.color_main == color)
        total = await self.session.scalar(
            select(func.count()).select_from(WardrobeItem).where(*filters)
        )
        result = await self.session.scalars(
            select(WardrobeItem)
            .where(*filters)
            .order_by(WardrobeItem.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result), int(total or 0)

    async def soft_delete(self, item: WardrobeItem) -> None:
        item.deleted_at = datetime.now(UTC)
        await self.session.flush()

    async def flush(self) -> None:
        await self.session.flush()
