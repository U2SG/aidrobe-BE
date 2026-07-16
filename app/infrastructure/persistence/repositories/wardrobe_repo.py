import uuid
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.persistence.models.wardrobe import WardrobeItem, WardrobeItemImage


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
        query: str | None = None,
        status: str | None = None,
        season: str | None = None,
        color: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> tuple[list[WardrobeItem], int]:
        filters = [WardrobeItem.user_id == user_id, WardrobeItem.deleted_at.is_(None)]
        if category is not None:
            filters.append(WardrobeItem.category == category)
        if query is not None and (normalized_query := query.strip()):
            escaped_query = (
                normalized_query.replace("\\", "\\\\")
                .replace("%", "\\%")
                .replace("_", "\\_")
            )
            pattern = f"%{escaped_query}%"
            filters.append(
                or_(
                    WardrobeItem.title.ilike(pattern, escape="\\"),
                    WardrobeItem.canonical_name.ilike(pattern, escape="\\"),
                    WardrobeItem.color_main.ilike(pattern, escape="\\"),
                    WardrobeItem.category.ilike(pattern, escape="\\"),
                )
            )
        if status is not None:
            filters.append(WardrobeItem.status == status)
        if season is not None:
            # season_tags JSONB shape: {"tags": ["spring", ...]} (V3 M14)
            # Navigate to the tags key first, then use @> array containment.
            filters.append(WardrobeItem.season_tags["tags"].contains([season]))
        if color is not None:
            filters.append(WardrobeItem.color_main == color)
        total = await self.session.scalar(
            select(func.count()).select_from(WardrobeItem).where(*filters)
        )
        result = await self.session.scalars(
            select(WardrobeItem)
            .where(*filters)
            .order_by(WardrobeItem.created_at.desc(), WardrobeItem.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result), int(total or 0)

    async def soft_delete(self, item: WardrobeItem) -> None:
        item.deleted_at = datetime.now(UTC)
        await self.session.flush()

    async def flush(self) -> None:
        await self.session.flush()
