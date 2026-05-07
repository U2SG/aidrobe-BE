import uuid
from datetime import UTC, date, datetime, time, timedelta
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
                WardrobeItem.fit_status != "no_longer_fits",
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
            if await self.daily_has_engagement(existing.id):
                return existing
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

    async def insert_wear_history_idempotent(
        self, wear_history: WearHistory
    ) -> tuple[WearHistory, bool]:
        """Insert wear_history with ON CONFLICT DO NOTHING on (user_id, outfit_id, worn_date).

        Returns (row, inserted) — when conflict, returns the existing row and inserted=False.
        """
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = (
            pg_insert(WearHistory)
            .values(
                user_id=wear_history.user_id,
                outfit_id=wear_history.outfit_id,
                selected_at=wear_history.selected_at,
                worn_date=wear_history.worn_date,
                created_at=wear_history.created_at,
                ootd_image_url=wear_history.ootd_image_url,
            )
            .on_conflict_do_nothing(
                index_elements=["user_id", "outfit_id", "worn_date"]
            )
            .returning(WearHistory.id)
        )
        result = await self.session.execute(stmt)
        new_id = result.scalar_one_or_none()
        if new_id is not None:
            await self.session.flush()
            row = await self.session.get(WearHistory, new_id)
            assert row is not None
            return row, True
        existing = await self.get_wear_history(
            wear_history.outfit_id, wear_history.user_id, wear_history.worn_date
        )
        assert existing is not None, "ON CONFLICT path but no existing row found"
        return existing, False

    async def daily_has_engagement(self, daily_recommendation_id: uuid.UUID) -> bool:
        feedback_id = await self.session.scalar(
            select(OutfitFeedback.id)
            .join(Outfit, Outfit.id == OutfitFeedback.outfit_id)
            .where(Outfit.daily_recommendation_id == daily_recommendation_id)
            .limit(1)
        )
        if feedback_id is not None:
            return True
        wear_history_id = await self.session.scalar(
            select(WearHistory.id)
            .join(Outfit, Outfit.id == WearHistory.outfit_id)
            .where(Outfit.daily_recommendation_id == daily_recommendation_id)
            .limit(1)
        )
        return wear_history_id is not None

    async def add_feedback(self, feedback: OutfitFeedback) -> OutfitFeedback:
        self.session.add(feedback)
        await self.session.flush()
        return feedback

    async def get_feedback_for_user(
        self, outfit_id: uuid.UUID, user_id: uuid.UUID
    ) -> OutfitFeedback | None:
        return cast(
            OutfitFeedback | None,
            await self.session.scalar(
                select(OutfitFeedback).where(
                    OutfitFeedback.outfit_id == outfit_id,
                    OutfitFeedback.user_id == user_id,
                )
            ),
        )

    async def get_outfit_history(
        self,
        user_id: uuid.UUID,
        start_date: date | None,
        end_date: date | None,
        limit: int,
        offset: int,
    ) -> list[tuple[WearHistory, Outfit, OutfitFeedback | None]]:
        statement = (
            select(WearHistory, Outfit, OutfitFeedback)
            .join(Outfit, Outfit.id == WearHistory.outfit_id)
            .outerjoin(
                OutfitFeedback,
                (OutfitFeedback.outfit_id == Outfit.id) & (OutfitFeedback.user_id == user_id),
            )
            .where(WearHistory.user_id == user_id)
            .order_by(WearHistory.worn_date.desc(), WearHistory.selected_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if start_date is not None:
            statement = statement.where(WearHistory.worn_date >= start_date)
        if end_date is not None:
            statement = statement.where(WearHistory.worn_date <= end_date)
        rows = await self.session.execute(statement)
        return [(row[0], row[1], row[2]) for row in rows.all()]

    async def users_without_departure_time(self) -> list[User]:
        result = await self.session.scalars(
            select(User)
            .options(selectinload(User.settings))
            .outerjoin(UserSettings)
            .where(UserSettings.departure_time.is_(None))
        )
        return list(result)

    async def users_due_for_departure_generation(self, now: datetime) -> list[User]:
        result = await self.session.scalars(
            select(User)
            .options(selectinload(User.settings))
            .join(UserSettings)
            .where(UserSettings.departure_time.is_not(None))
        )
        now_time = now.time()
        return [
            user
            for user in result
            if user.settings is not None
            and user.settings.departure_time is not None
            and _time_in_next_window(user.settings.departure_time, now_time, minutes=90)
        ]

    async def get_recent_wear_history(
        self, user_id: uuid.UUID, days: int = 30
    ) -> list[WearHistory]:
        since = datetime.now(UTC).date() - timedelta(days=days)
        result = await self.session.scalars(
            select(WearHistory).where(
                WearHistory.user_id == user_id,
                WearHistory.worn_date >= since,
            )
        )
        return list(result)

    async def get_user_location(self, user_id: uuid.UUID) -> str | None:
        return await self.session.scalar(
            select(UserSettings.location).where(UserSettings.user_id == user_id)
        )

    async def get_recent_worn_outfit_item_ids(
        self, user_id: uuid.UUID, target_date: date, days: int = 7
    ) -> list[set[str]]:
        since = target_date - timedelta(days=days)
        rows = await self.session.execute(
            select(WearHistory.outfit_id, OutfitItem.item_id)
            .join(OutfitItem, OutfitItem.outfit_id == WearHistory.outfit_id)
            .where(
                WearHistory.user_id == user_id,
                WearHistory.worn_date >= since,
                WearHistory.worn_date < target_date,
            )
        )
        outfit_items: dict[uuid.UUID, set[str]] = {}
        for outfit_id, item_id in rows:
            outfit_items.setdefault(outfit_id, set()).add(str(item_id))
        return list(outfit_items.values())

    async def get_recent_feedback(
        self, user_id: uuid.UUID, days: int = 30
    ) -> list[OutfitFeedback]:
        since = datetime.now(UTC) - timedelta(days=days)
        result = await self.session.scalars(
            select(OutfitFeedback).where(
                OutfitFeedback.user_id == user_id,
                OutfitFeedback.created_at >= since,
            )
        )
        return list(result)

    async def get_outfit_main_item_ids(
        self, outfit_ids: list[uuid.UUID]
    ) -> dict[str, str]:
        """Return {outfit_id_str: main_wardrobe_item_id_str} for the given outfit ids.

        Main item is the first OutfitItem with role 'top' or 'dress', falling back to
        the lowest display_order item.
        """
        if not outfit_ids:
            return {}
        rows = await self.session.execute(
            select(OutfitItem.outfit_id, OutfitItem.item_id, OutfitItem.role)
            .where(OutfitItem.outfit_id.in_(outfit_ids))
            .order_by(OutfitItem.outfit_id, OutfitItem.display_order)
        )
        outfit_map: dict[str, str] = {}
        for outfit_id, item_id, role in rows:
            key = str(outfit_id)
            if key not in outfit_map:
                outfit_map[key] = str(item_id)
            if role in {"top", "dress"}:
                outfit_map[key] = str(item_id)
        return outfit_map


def _time_in_next_window(target: time, start: time, minutes: int) -> bool:
    target_minute = target.hour * 60 + target.minute
    start_minute = start.hour * 60 + start.minute
    delta = (target_minute - start_minute) % (24 * 60)
    return 0 <= delta <= minutes
