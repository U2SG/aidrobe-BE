import uuid
from datetime import date
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.persistence.models.packing_list import PackingList
from app.infrastructure.persistence.models.recommendation import OutfitItem, WearHistory
from app.infrastructure.persistence.models.wardrobe import WardrobeItem


class SqlAlchemyPackingListRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, packing_list: PackingList) -> PackingList:
        self.session.add(packing_list)
        await self.session.flush()
        return packing_list

    async def update(self, packing_list: PackingList) -> PackingList:
        packing_list.updated_at = func.now()
        await self.session.flush()
        return packing_list

    async def get_for_user(
        self, packing_list_id: uuid.UUID, user_id: uuid.UUID
    ) -> PackingList | None:
        return cast(
            PackingList | None,
            await self.session.scalar(
                select(PackingList).where(
                    PackingList.id == packing_list_id,
                    PackingList.user_id == user_id,
                )
            ),
        )

    async def list_for_user(self, user_id: uuid.UUID) -> list[PackingList]:
        result = await self.session.scalars(
            select(PackingList)
            .where(PackingList.user_id == user_id)
            .order_by(PackingList.created_at.desc())
        )
        return list(result)

    async def list_candidate_items(self, user_id: uuid.UUID) -> list[WardrobeItem]:
        """Wearable, non-deleted wardrobe items, best-fit first.

        Filtering is pushed down to SQL. Ordering prefers fit_status=fits_well
        so the generator can take the strongest candidates per category without
        re-sorting in Python.
        """
        result = await self.session.scalars(
            select(WardrobeItem)
            .where(
                WardrobeItem.user_id == user_id,
                WardrobeItem.deleted_at.is_(None),
                WardrobeItem.status == "wearable",
            )
            .order_by(
                (WardrobeItem.fit_status == "fits_well").desc(),
                WardrobeItem.last_worn_at.asc().nulls_first(),
                WardrobeItem.created_at.desc(),
            )
        )
        return list(result)

    async def recent_worn_item_ids(
        self, user_id: uuid.UUID, since: date, until: date | None = None
    ) -> set[uuid.UUID]:
        """Item ids worn (via any outfit) in `[since, until]` — for the 7-day skip.

        `until` (the trip anchor) bounds the window so a future-dated wear row
        (the wear endpoint accepts a caller-supplied `worn_date`) can't be counted
        as "worn before the trip" and wrongly drop an item from the candidates."""
        conditions = [WearHistory.user_id == user_id, WearHistory.worn_date >= since]
        if until is not None:
            conditions.append(WearHistory.worn_date <= until)
        result = await self.session.scalars(
            select(OutfitItem.item_id)
            .join(WearHistory, WearHistory.outfit_id == OutfitItem.outfit_id)
            .where(*conditions)
        )
        return set(result)
