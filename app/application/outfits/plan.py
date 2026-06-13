"""V4 Outfit Calendar use cases.

Plans reference existing `outfits.id` (the system has no self-built outfits).
A day can hold multiple plans; status soft-cancels. Marking a plan worn
delegates to WearOutfit (lands WearHistory + bumps wear_count) and then
cancels the plan — wear logic is never re-implemented here.
"""

import uuid
from dataclasses import dataclass, field
from datetime import date

from app.application.outfits.wear import WearOutfit
from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.infrastructure.persistence.models.recommendation import OutfitPlan, WearHistory
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)


@dataclass
class DayCellOutfit:
    outfit_id: uuid.UUID
    outfit_type: str
    title: str
    reason_text: str


@dataclass
class DayCell:
    kind: str  # "worn" | "planned"
    outfit: DayCellOutfit | None = None
    # worn
    wear_history_id: uuid.UUID | None = None
    ootd_image_url: str | None = None
    # planned
    outfit_plan_id: uuid.UUID | None = None
    status: str | None = None


@dataclass
class CalendarDay:
    cell_date: date
    cells: list[DayCell] = field(default_factory=list)


class CreateOutfitPlan:
    def __init__(self, repository: SqlAlchemyRecommendationRepository) -> None:
        self.repository = repository

    async def execute(
        self, user_id: uuid.UUID, outfit_id: uuid.UUID, planned_date: date
    ) -> OutfitPlan:
        outfit = await self.repository.get_outfit_for_user(outfit_id, user_id)
        if outfit is None:
            raise FriendlyException(error_codes.NOT_FOUND, "Outfit not found", status_code=404)
        plan = OutfitPlan(
            user_id=user_id,
            outfit_id=outfit_id,
            planned_date=planned_date,
            status="planned",
        )
        return await self.repository.add_outfit_plan(plan)


class ReschedulePlan:
    def __init__(self, repository: SqlAlchemyRecommendationRepository) -> None:
        self.repository = repository

    async def execute(
        self, plan_id: uuid.UUID, user_id: uuid.UUID, planned_date: date
    ) -> OutfitPlan:
        plan = await self.repository.get_outfit_plan_for_user(plan_id, user_id)
        if plan is None:
            raise FriendlyException(error_codes.NOT_FOUND, "Plan not found", status_code=404)
        plan.planned_date = planned_date
        return await self.repository.update_outfit_plan(plan)


class CancelPlan:
    def __init__(self, repository: SqlAlchemyRecommendationRepository) -> None:
        self.repository = repository

    async def execute(self, plan_id: uuid.UUID, user_id: uuid.UUID) -> OutfitPlan:
        plan = await self.repository.get_outfit_plan_for_user(plan_id, user_id)
        if plan is None:
            raise FriendlyException(error_codes.NOT_FOUND, "Plan not found", status_code=404)
        plan.status = "cancelled"
        return await self.repository.update_outfit_plan(plan)


class MarkPlanWorn:
    def __init__(self, repository: SqlAlchemyRecommendationRepository) -> None:
        self.repository = repository

    async def execute(self, plan_id: uuid.UUID, user_id: uuid.UUID) -> WearHistory:
        plan = await self.repository.get_outfit_plan_for_user(plan_id, user_id)
        if plan is None:
            raise FriendlyException(error_codes.NOT_FOUND, "Plan not found", status_code=404)
        # Delegate to the existing wear path: lands WearHistory + bumps
        # wear_count. Idempotent (returns existing row if already worn).
        wear_history = await WearOutfit(self.repository).execute(
            plan.outfit_id, user_id, plan.planned_date
        )
        plan.status = "cancelled"
        await self.repository.update_outfit_plan(plan)
        return wear_history


class GetOutfitCalendar:
    def __init__(self, repository: SqlAlchemyRecommendationRepository) -> None:
        self.repository = repository

    async def execute(
        self, user_id: uuid.UUID, start: date, end: date
    ) -> list[CalendarDay]:
        plans = await self.repository.list_plans_in_range(user_id, start, end, "planned")
        wears = await self.repository.list_wear_history_in_range(user_id, start, end)

        # Batch-load all referenced outfits once (avoid N+1).
        outfit_ids = list({p.outfit_id for p in plans} | {w.outfit_id for w in wears})
        outfits = await self.repository.get_outfit_summaries(outfit_ids)

        def _summary(outfit_id: uuid.UUID) -> DayCellOutfit | None:
            o = outfits.get(outfit_id)
            if o is None:
                return None
            return DayCellOutfit(
                outfit_id=o.id,
                outfit_type=o.outfit_type,
                title=o.title,
                reason_text=o.reason_text,
            )

        by_date: dict[date, CalendarDay] = {}

        def _day(d: date) -> CalendarDay:
            day = by_date.get(d)
            if day is None:
                day = CalendarDay(cell_date=d)
                by_date[d] = day
            return day

        for w in wears:
            _day(w.worn_date).cells.append(
                DayCell(
                    kind="worn",
                    outfit=_summary(w.outfit_id),
                    wear_history_id=w.id,
                    ootd_image_url=w.ootd_image_url,
                )
            )
        for p in plans:
            _day(p.planned_date).cells.append(
                DayCell(
                    kind="planned",
                    outfit=_summary(p.outfit_id),
                    outfit_plan_id=p.id,
                    status=p.status,
                )
            )

        return [by_date[d] for d in sorted(by_date)]
