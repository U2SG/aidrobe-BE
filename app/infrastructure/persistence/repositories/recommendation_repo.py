import uuid
from datetime import date, datetime, timedelta
from typing import cast

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.common.enums import ItemStatus
from app.infrastructure.persistence.models.recommendation import (
    DailyRecommendation,
    Outfit,
    OutfitFeedback,
    OutfitItem,
    WearHistory,
)
from app.infrastructure.persistence.models.user import User, UserSettings
from app.infrastructure.persistence.models.wardrobe import WardrobeItem


class SqlAlchemyRecommendationRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_wearable_items(self, user_id: uuid.UUID) -> list[WardrobeItem]:
        result = await self.session.scalars(
            select(WardrobeItem).where(
                WardrobeItem.user_id == user_id,
                WardrobeItem.status == ItemStatus.WEARABLE.value,
                WardrobeItem.deleted_at.is_(None),
            )
        )
        return list(result)

    async def recent_worn_main_item_ids(self, user_id: uuid.UUID, target_date: date) -> set[str]:
        since = target_date - timedelta(days=3)
        result = await self.session.scalars(
            select(OutfitItem.item_id)
            .join(Outfit, Outfit.id == OutfitItem.outfit_id)
            .join(WearHistory, WearHistory.outfit_id == Outfit.id)
            .where(WearHistory.user_id == user_id, WearHistory.worn_date >= since)
        )
        return {str(item_id) for item_id in result}

    async def get_daily(
        self, user_id: uuid.UUID, recommendation_date: date
    ) -> DailyRecommendation | None:
        return cast(
            DailyRecommendation | None,
            await self.session.scalar(
            select(DailyRecommendation)
            .options(selectinload(DailyRecommendation.outfits).selectinload(Outfit.items))
            .where(
                DailyRecommendation.user_id == user_id,
                DailyRecommendation.recommendation_date == recommendation_date,
            )
            ),
        )

    async def get_latest_before(
        self, user_id: uuid.UUID, target_date: date
    ) -> DailyRecommendation | None:
        return cast(
            DailyRecommendation | None,
            await self.session.scalar(
            select(DailyRecommendation)
            .options(selectinload(DailyRecommendation.outfits).selectinload(Outfit.items))
            .where(
                DailyRecommendation.user_id == user_id,
                DailyRecommendation.recommendation_date < target_date,
            )
            .order_by(DailyRecommendation.recommendation_date.desc())
            .limit(1)
            ),
        )

    async def replace_daily(self, daily: DailyRecommendation) -> DailyRecommendation:
        existing = await self.get_daily(daily.user_id, daily.recommendation_date)
        if existing is not None:
            await self.session.execute(
                delete(Outfit).where(Outfit.daily_recommendation_id == existing.id)
            )
            existing.status = daily.status
            existing.generation_source = daily.generation_source
            existing.score_breakdown = daily.score_breakdown
            await self.session.flush()
            return existing
        self.session.add(daily)
        await self.session.flush()
        return daily

    async def add_outfit(self, outfit: Outfit) -> Outfit:
        self.session.add(outfit)
        await self.session.flush()
        return outfit

    async def add_outfit_item(self, outfit_item: OutfitItem) -> OutfitItem:
        self.session.add(outfit_item)
        await self.session.flush()
        return outfit_item

    async def get_outfit_for_user(self, outfit_id: uuid.UUID, user_id: uuid.UUID) -> Outfit | None:
        return cast(
            Outfit | None,
            await self.session.scalar(
            select(Outfit)
            .options(selectinload(Outfit.items))
            .where(Outfit.id == outfit_id, Outfit.user_id == user_id)
            ),
        )

    async def get_wear_history(
        self, outfit_id: uuid.UUID, user_id: uuid.UUID, worn_date: date
    ) -> WearHistory | None:
        return cast(
            WearHistory | None,
            await self.session.scalar(
            select(WearHistory).where(
                WearHistory.outfit_id == outfit_id,
                WearHistory.user_id == user_id,
                WearHistory.worn_date == worn_date,
            )
            ),
        )

    async def add_wear_history(self, wear_history: WearHistory) -> WearHistory:
        self.session.add(wear_history)
        await self.session.flush()
        return wear_history

    async def add_feedback(self, feedback: OutfitFeedback) -> OutfitFeedback:
        self.session.add(feedback)
        await self.session.flush()
        return feedback

    async def users_without_departure_time(self) -> list[User]:
        result = await self.session.scalars(
            select(User).outerjoin(UserSettings).where(UserSettings.departure_time.is_(None))
        )
        return list(result)

    async def users_due_for_departure_generation(self, now: datetime) -> list[User]:
        result = await self.session.scalars(select(User).join(UserSettings))
        return list(result)
