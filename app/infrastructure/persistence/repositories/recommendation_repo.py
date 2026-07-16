import hashlib
import uuid
from datetime import UTC, date, datetime, time, timedelta
from typing import cast

from sqlalchemy import delete, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.common.enums import ItemStatus, RecommendationEventType
from app.common.local_time import local_datetime
from app.infrastructure.persistence.models.recommendation import (
    DailyRecommendation,
    Outfit,
    OutfitFavorite,
    OutfitFeedback,
    OutfitItem,
    OutfitPlan,
    RecommendationEvent,
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

    async def list_forgotten_wearable_items(
        self, user_id: uuid.UUID, cutoff: date, limit: int = 20
    ) -> list[WardrobeItem]:
        """BE#80 — wearable items never worn or not worn since `cutoff`, most
        forgotten first (nulls = never worn, surfaced first)."""
        result = await self.session.scalars(
            select(WardrobeItem)
            .where(
                WardrobeItem.user_id == user_id,
                WardrobeItem.status == ItemStatus.WEARABLE.value,
                WardrobeItem.fit_status != "no_longer_fits",
                WardrobeItem.deleted_at.is_(None),
                or_(
                    WardrobeItem.last_worn_at.is_(None),
                    WardrobeItem.last_worn_at < cutoff,
                ),
            )
            .order_by(WardrobeItem.last_worn_at.asc().nulls_first())
            .limit(limit)
        )
        return list(result)

    async def recently_resurfaced_item_ids(
        self, user_id: uuid.UUID, since: datetime
    ) -> set[uuid.UUID]:
        """BE#80 — item ids already surfaced as a resurface insight since `since`
        (FE emits resurface_insight_viewed), so BE doesn't re-spam the same item."""
        result = await self.session.scalars(
            select(RecommendationEvent.item_id).where(
                RecommendationEvent.user_id == user_id,
                RecommendationEvent.event_type
                == RecommendationEventType.RESURFACE_INSIGHT_VIEWED.value,
                RecommendationEvent.created_at >= since,
                RecommendationEvent.item_id.is_not(None),
            )
        )
        return {item_id for item_id in result if item_id is not None}

    async def recent_worn_main_item_ids(self, user_id: uuid.UUID, target_date: date) -> set[str]:
        since = target_date - timedelta(days=3)
        result = await self.session.scalars(
            select(OutfitItem.item_id)
            .join(Outfit, Outfit.id == OutfitItem.outfit_id)
            .join(WearHistory, WearHistory.outfit_id == Outfit.id)
            .where(WearHistory.user_id == user_id, WearHistory.worn_date >= since)
        )
        return {str(item_id) for item_id in result}

    async def get_daily(self, user_id: uuid.UUID, recommendation_date: date) -> DailyRecommendation | None:
        # Filter to is_current=True so reads always see the latest revision.
        # Legacy single-row data already has is_current default=True so this
        # doesn't change behavior for pre-revision rows.
        return cast(
            DailyRecommendation | None,
            await self.session.scalar(
                select(DailyRecommendation)
                .options(selectinload(DailyRecommendation.outfits).selectinload(Outfit.items))
                .where(
                    DailyRecommendation.user_id == user_id,
                    DailyRecommendation.recommendation_date == recommendation_date,
                    DailyRecommendation.is_current.is_(True),
                )
            ),
        )

    async def daily_recommendation_exists_for_user(
        self, daily_recommendation_id: uuid.UUID, user_id: uuid.UUID
    ) -> bool:
        """BE#81 — ownership guard for event attribution: a client-supplied
        recommendation_id must belong to this user before we store it (else an FK
        IntegrityError on a stale id, or cross-user attribution on a foreign one)."""
        return (
            await self.session.scalar(
                select(DailyRecommendation.id).where(
                    DailyRecommendation.id == daily_recommendation_id,
                    DailyRecommendation.user_id == user_id,
                )
            )
        ) is not None

    async def count_revisions_for_date(self, user_id: uuid.UUID, recommendation_date: date) -> int:
        """How many daily rows exist for this (user, date) regardless of
        is_current. Used by the refresh rate limit so 3 refreshes ⇒ 4 rows
        (rev 1 from background gen + 3 from refresh)."""
        from sqlalchemy import func

        return cast(
            int,
            await self.session.scalar(
                select(func.count())
                .select_from(DailyRecommendation)
                .where(
                    DailyRecommendation.user_id == user_id,
                    DailyRecommendation.recommendation_date == recommendation_date,
                )
            )
            or 0,
        )

    async def lock_daily_write(
        self, user_id: uuid.UUID, recommendation_date: date
    ) -> None:
        """Enter the shared user/date write critical section for this transaction."""
        await self._lock_daily_write(user_id, recommendation_date)

    async def archive_current_and_revise(self, daily: DailyRecommendation) -> DailyRecommendation:
        """Phase D3 — refresh path. Flips the current row to is_current=False
        (engagement, wear_history, feedback all stay attached to the OLD
        outfit_ids — that's the whole point of versioning), then inserts a
        new row with revision = max+1, is_current=True. The caller still
        adds outfits to the returned new row.

        Distinct from `replace_daily` (which OVERWRITES in-place when there
        is no engagement). The two paths exist on purpose: background
        recompute ⇒ replace; user-initiated refresh ⇒ archive+revise.

        Concurrency: takes a Postgres advisory lock keyed by
        (user_id, recommendation_date). The lock holds for the outer
        transaction so the UPDATE → max-read → INSERT sequence is
        linearizable. Advisory locks (vs SELECT FOR UPDATE) cover the
        first-refresh case where no current row yet exists — there is
        nothing to lock at row level, but the advisory key is independent
        of row existence."""
        from sqlalchemy import func

        await self._lock_daily_write(daily.user_id, daily.recommendation_date)

        # Under the lock now: serialize UPDATE → INSERT.
        await self.session.execute(
            update(DailyRecommendation)
            .where(
                DailyRecommendation.user_id == daily.user_id,
                DailyRecommendation.recommendation_date == daily.recommendation_date,
                DailyRecommendation.is_current.is_(True),
            )
            .values(is_current=False)
        )
        max_rev = (
            await self.session.scalar(
                select(func.coalesce(func.max(DailyRecommendation.revision), 0)).where(
                    DailyRecommendation.user_id == daily.user_id,
                    DailyRecommendation.recommendation_date == daily.recommendation_date,
                )
            )
            or 0
        )
        daily.revision = max_rev + 1
        daily.is_current = True
        self.session.add(daily)
        await self.session.flush()
        return daily

    async def get_latest_before(self, user_id: uuid.UUID, target_date: date) -> DailyRecommendation | None:
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

    async def replace_daily(
        self,
        daily: DailyRecommendation,
        *,
        preserve_refresh_after: datetime | None = None,
    ) -> tuple[DailyRecommendation, bool]:
        """Replace an unengaged daily and report whether outfits should be written.

        The final guard runs under the shared daily advisory lock. This prevents
        an older background job from overwriting a user refresh that committed
        while the job was generating outside the lock.
        """
        await self._lock_daily_write(daily.user_id, daily.recommendation_date)
        existing = await self.get_daily(daily.user_id, daily.recommendation_date)
        if existing is not None:
            if (
                preserve_refresh_after is not None
                and existing.generation_source == "refresh"
                and existing.updated_at is not None
            ):
                updated_at = existing.updated_at
                if updated_at.tzinfo is None:
                    updated_at = updated_at.replace(tzinfo=UTC)
                if updated_at >= preserve_refresh_after:
                    return existing, False
            if await self.daily_has_engagement(existing.id):
                return existing, False
            await self.session.execute(delete(Outfit).where(Outfit.daily_recommendation_id == existing.id))
            existing.status = daily.status
            existing.generation_source = daily.generation_source
            existing.score_breakdown = daily.score_breakdown
            existing.context_json = daily.context_json
            await self.session.flush()
            return existing, True
        self.session.add(daily)
        await self.session.flush()
        return daily, True

    async def _lock_daily_write(
        self, user_id: uuid.UUID, recommendation_date: date
    ) -> None:
        # Refresh and background replacement share one lock namespace so their
        # update/insert paths cannot race each other or create split current rows.
        digest = hashlib.sha1(
            f"daily:{user_id}:{recommendation_date.isoformat()}".encode()
        ).digest()[:8]
        lock_key = int.from_bytes(digest, "big", signed=True)
        await self.session.execute(
            text("SELECT pg_advisory_xact_lock(:key)"), {"key": lock_key}
        )

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

    async def add_outfit_favorite(self, user_id: uuid.UUID, outfit_id: uuid.UUID) -> None:
        """Idempotently favorite an outfit for a user."""
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = (
            pg_insert(OutfitFavorite)
            .values(user_id=user_id, outfit_id=outfit_id)
            .on_conflict_do_nothing(index_elements=["user_id", "outfit_id"])
        )
        await self.session.execute(stmt)
        await self.session.flush()

    async def remove_outfit_favorite(self, user_id: uuid.UUID, outfit_id: uuid.UUID) -> None:
        """Idempotently remove an outfit favorite."""
        await self.session.execute(
            delete(OutfitFavorite).where(
                OutfitFavorite.user_id == user_id,
                OutfitFavorite.outfit_id == outfit_id,
            )
        )
        await self.session.flush()

    async def list_favorited_outfit_ids(self, user_id: uuid.UUID, outfit_ids: list[uuid.UUID]) -> set[uuid.UUID]:
        if not outfit_ids:
            return set()
        result = await self.session.scalars(
            select(OutfitFavorite.outfit_id).where(
                OutfitFavorite.user_id == user_id,
                OutfitFavorite.outfit_id.in_(outfit_ids),
            )
        )
        return set(result.all())

    async def get_wear_history(self, outfit_id: uuid.UUID, user_id: uuid.UUID, worn_date: date) -> WearHistory | None:
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

    async def insert_wear_history_idempotent(self, wear_history: WearHistory) -> tuple[WearHistory, bool]:
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
            .on_conflict_do_nothing(index_elements=["user_id", "outfit_id", "worn_date"])
            .returning(WearHistory.id)
        )
        result = await self.session.execute(stmt)
        new_id = result.scalar_one_or_none()
        if new_id is not None:
            await self.session.flush()
            row = await self.session.get(WearHistory, new_id)
            assert row is not None
            return row, True
        existing = await self.get_wear_history(wear_history.outfit_id, wear_history.user_id, wear_history.worn_date)
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
        if wear_history_id is not None:
            return True
        # A favorited outfit is engagement too — otherwise a background recompute
        # could replace_daily() and cascade-delete the user's saved favorite.
        favorite_outfit_id = await self.session.scalar(
            select(OutfitFavorite.outfit_id)
            .join(Outfit, Outfit.id == OutfitFavorite.outfit_id)
            .where(Outfit.daily_recommendation_id == daily_recommendation_id)
            .limit(1)
        )
        return favorite_outfit_id is not None

    async def add_feedback(self, feedback: OutfitFeedback) -> OutfitFeedback:
        self.session.add(feedback)
        await self.session.flush()
        return feedback

    async def add_recommendation_event(self, event: RecommendationEvent) -> RecommendationEvent:
        """BE#40 — append one interaction event. Caller owns commit."""
        self.session.add(event)
        await self.session.flush()
        return event

    async def list_recommendation_events(
        self,
        user_id: uuid.UUID,
        *,
        since: datetime | None = None,
        until: datetime | None = None,
        event_type: str | None = None,
        limit: int = 200,
    ) -> list[RecommendationEvent]:
        """Query a user's events by time window (+ optional type), newest first —
        backed by the (user_id, [event_type,] created_at) indexes."""
        stmt = select(RecommendationEvent).where(RecommendationEvent.user_id == user_id)
        if since is not None:
            stmt = stmt.where(RecommendationEvent.created_at >= since)
        if until is not None:
            stmt = stmt.where(RecommendationEvent.created_at <= until)
        if event_type is not None:
            stmt = stmt.where(RecommendationEvent.event_type == event_type)
        stmt = stmt.order_by(RecommendationEvent.created_at.desc()).limit(limit)
        return list(await self.session.scalars(stmt))

    async def get_feedback_for_user(self, outfit_id: uuid.UUID, user_id: uuid.UUID) -> OutfitFeedback | None:
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

    async def users_due_for_departure_generation(
        self, now: datetime, default_timezone: str
    ) -> list[User]:
        result = await self.session.scalars(
            select(User)
            .options(selectinload(User.settings))
            .join(UserSettings)
            .where(UserSettings.departure_time.is_not(None))
        )
        return [
            user
            for user in result
            if user.settings is not None
            and user.settings.departure_time is not None
            and _time_in_next_window(
                user.settings.departure_time,
                local_datetime(now, user.settings.timezone, default_timezone).time(),
                minutes=90,
            )
        ]

    async def get_recent_wear_history(
        self,
        user_id: uuid.UUID,
        target_date: date,
        days: int = 30,
    ) -> list[WearHistory]:
        since = target_date - timedelta(days=days)
        result = await self.session.scalars(
            select(WearHistory).where(
                WearHistory.user_id == user_id,
                WearHistory.worn_date >= since,
            )
        )
        return list(result)

    async def get_user_location(self, user_id: uuid.UUID) -> str | None:
        return await self.session.scalar(select(UserSettings.location).where(UserSettings.user_id == user_id))

    async def get_user_timezone(self, user_id: uuid.UUID) -> str | None:
        return await self.session.scalar(
            select(UserSettings.timezone).where(UserSettings.user_id == user_id)
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

    async def get_recent_recommended_outfit_item_ids(
        self, user_id: uuid.UUID, target_date: date, days: int = 7
    ) -> list[set[str]]:
        """BE#39 — item-sets of outfits RECOMMENDED (current revision) within the
        window, so diversity avoids repeating a look the user was recently shown,
        not only ones they wore. Excludes today (recommendation_date < target)."""
        since = target_date - timedelta(days=days)
        rows = await self.session.execute(
            select(Outfit.id, OutfitItem.item_id)
            .join(DailyRecommendation, DailyRecommendation.id == Outfit.daily_recommendation_id)
            .join(OutfitItem, OutfitItem.outfit_id == Outfit.id)
            .where(
                DailyRecommendation.user_id == user_id,
                DailyRecommendation.is_current.is_(True),
                DailyRecommendation.recommendation_date >= since,
                DailyRecommendation.recommendation_date < target_date,
            )
        )
        outfit_items: dict[uuid.UUID, set[str]] = {}
        for outfit_id, item_id in rows:
            outfit_items.setdefault(outfit_id, set()).add(str(item_id))
        return list(outfit_items.values())

    async def recent_recommended_main_item_ids(self, user_id: uuid.UUID, target_date: date, days: int = 7) -> set[str]:
        """BE#39 — hero (top/dress) item ids RECOMMENDED within the window, so
        the freshness signal also rotates recently-recommended hero pieces, not
        only worn ones."""
        since = target_date - timedelta(days=days)
        result = await self.session.scalars(
            select(OutfitItem.item_id)
            .join(Outfit, Outfit.id == OutfitItem.outfit_id)
            .join(DailyRecommendation, DailyRecommendation.id == Outfit.daily_recommendation_id)
            .where(
                DailyRecommendation.user_id == user_id,
                DailyRecommendation.is_current.is_(True),
                DailyRecommendation.recommendation_date >= since,
                DailyRecommendation.recommendation_date < target_date,
                OutfitItem.role.in_(["top", "dress"]),
            )
        )
        return {str(item_id) for item_id in result}

    async def get_recent_feedback(self, user_id: uuid.UUID, days: int = 30) -> list[OutfitFeedback]:
        since = datetime.now(UTC) - timedelta(days=days)
        result = await self.session.scalars(
            select(OutfitFeedback).where(
                OutfitFeedback.user_id == user_id,
                OutfitFeedback.created_at >= since,
            )
        )
        return list(result)

    async def get_outfit_main_item_ids(self, outfit_ids: list[uuid.UUID]) -> dict[str, str]:
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

    # --- V4 Outfit Calendar ---------------------------------------------

    async def add_outfit_plan(self, plan: OutfitPlan) -> OutfitPlan:
        self.session.add(plan)
        await self.session.flush()
        return plan

    async def get_outfit_plan_for_user(self, plan_id: uuid.UUID, user_id: uuid.UUID) -> OutfitPlan | None:
        return cast(
            OutfitPlan | None,
            await self.session.scalar(
                select(OutfitPlan).where(OutfitPlan.id == plan_id, OutfitPlan.user_id == user_id)
            ),
        )

    async def list_plans_in_range(
        self,
        user_id: uuid.UUID,
        start: date,
        end: date,
        status: str = "planned",
    ) -> list[OutfitPlan]:
        result = await self.session.scalars(
            select(OutfitPlan)
            .where(
                OutfitPlan.user_id == user_id,
                OutfitPlan.status == status,
                OutfitPlan.planned_date >= start,
                OutfitPlan.planned_date <= end,
            )
            .order_by(OutfitPlan.planned_date)
        )
        return list(result)

    async def list_wear_history_in_range(self, user_id: uuid.UUID, start: date, end: date) -> list[WearHistory]:
        result = await self.session.scalars(
            select(WearHistory)
            .where(
                WearHistory.user_id == user_id,
                WearHistory.worn_date >= start,
                WearHistory.worn_date <= end,
            )
            .order_by(WearHistory.worn_date)
        )
        return list(result)

    async def update_outfit_plan(self, plan: OutfitPlan) -> OutfitPlan:
        self.session.add(plan)
        await self.session.flush()
        # `updated_at` is server-side `onupdate=func.now()`; an UPDATE doesn't
        # RETURN it, so it's expired after flush. Reload inside the async
        # context — otherwise the caller's from_model() reads an expired
        # attribute and trips MissingGreenlet on the implicit sync load.
        await self.session.refresh(plan)
        return plan

    async def get_outfit_summaries(self, outfit_ids: list[uuid.UUID]) -> dict[uuid.UUID, Outfit]:
        """Batch-load outfits for calendar cells, avoiding N+1."""
        if not outfit_ids:
            return {}
        result = await self.session.scalars(select(Outfit).where(Outfit.id.in_(outfit_ids)))
        return {o.id: o for o in result}


def _time_in_next_window(target: time, start: time, minutes: int) -> bool:
    target_minute = target.hour * 60 + target.minute
    start_minute = start.hour * 60 + start.minute
    delta = (target_minute - start_minute) % (24 * 60)
    return 0 <= delta <= minutes
