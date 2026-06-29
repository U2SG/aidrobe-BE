"""M16 Travel Packing List use cases — rule-based, no LLM.

Generation walks days x scenes and picks wardrobe items per scene's expected
categories. It prefers status=wearable + fit_status=fits_well (the candidate
query already orders fits_well first) and skips items worn in the last 7 days
(via outfit wear history). The result is stored on `packing_lists.items` as a
JSONB array of {item_id, day, scene, notes}. Marking the list confirmed is a
plain status flip.
"""

import uuid
from datetime import date, timedelta

from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.config.settings import get_settings
from app.infrastructure.persistence.models.packing_list import PackingList
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.packing_list_repo import (
    SqlAlchemyPackingListRepository,
)

# Static scene -> expected categories map. Kept deliberately small; the goal is
# a sensible default selection per scene, not a styling engine. Categories are
# matched case-insensitively against WardrobeItem.category.
SCENE_CATEGORIES: dict[str, list[str]] = {
    "business": ["outerwear", "top", "bottom", "shoes"],
    "dinner": ["top", "bottom", "shoes"],
    "casual": ["top", "bottom", "shoes"],
    "sport": ["top", "bottom", "shoes"],
    "beach": ["top", "bottom", "shoes"],
    "formal": ["outerwear", "top", "bottom", "shoes"],
}
# Fallback when a scene is not recognised.
DEFAULT_CATEGORIES: list[str] = ["top", "bottom", "shoes"]

RECENT_WEAR_DAYS = 7


def _categories_for_scene(scene: str) -> list[str]:
    return SCENE_CATEGORIES.get(scene.lower(), DEFAULT_CATEGORIES)


class GeneratePackingList:
    def __init__(self, repository: SqlAlchemyPackingListRepository) -> None:
        self.repository = repository

    async def execute(
        self,
        user_id: uuid.UUID,
        destination: str | None,
        days: int,
        scenes: list[str],
        weather_hint: str | None,
        departure_date: date | None,
    ) -> PackingList:
        if days < 1:
            raise FriendlyException(
                error_codes.VALIDATION_ERROR, "days must be >= 1", status_code=422
            )
        if not scenes:
            raise FriendlyException(
                error_codes.VALIDATION_ERROR,
                "scenes must not be empty",
                status_code=422,
            )

        candidates = await self.repository.list_candidate_items(user_id)

        # Skip items worn in the 7 days before departure — anchored to the trip,
        # not today, so items packed for a future trip are still available now.
        # Applied to BOTH paths (BE#68 review): the planner must also respect DB
        # wear history, not only the looks it plans within this trip.
        anchor = departure_date if departure_date is not None else date.today()
        since = anchor - timedelta(days=RECENT_WEAR_DAYS)
        recently_worn = await self.repository.recent_worn_item_ids(user_id, since, until=anchor)
        candidates = [item for item in candidates if item.id not in recently_worn]

        # BE#68 — when the planner is on, build the rich per-day plan (engine +
        # preferences + graph), persist it, and derive the flat checklist from it
        # so both views stay consistent. Flag off → the legacy round-robin below.
        if get_settings().ENABLE_PACKING_PLANNER:
            return await self._generate_with_planner(
                user_id, destination, days, scenes, weather_hint, departure_date, candidates
            )

        by_category: dict[str, list[WardrobeItem]] = {}
        for item in candidates:
            by_category.setdefault(item.category.lower(), []).append(item)

        # Round-robin a pointer per category so successive days rotate items
        # rather than repeating the same piece every day.
        cursor: dict[str, int] = {}
        selected: list[dict[str, object]] = []
        for day in range(1, days + 1):
            for scene in scenes:
                for category in _categories_for_scene(scene):
                    pool = by_category.get(category)
                    if not pool:
                        continue
                    idx = cursor.get(category, 0)
                    item = pool[idx % len(pool)]
                    cursor[category] = idx + 1
                    note = "fits well" if item.fit_status == "fits_well" else ""
                    selected.append(
                        {
                            "item_id": str(item.id),
                            "day": day,
                            "scene": scene,
                            "notes": note,
                            "packed": False,
                        }
                    )

        packing_list = PackingList(
            user_id=user_id,
            destination=destination,
            days=days,
            scenes=list(scenes),
            departure_date=departure_date,
            items=selected,
            status="draft",
        )
        return await self.repository.add(packing_list)

    async def _generate_with_planner(
        self,
        user_id: uuid.UUID,
        destination: str | None,
        days: int,
        scenes: list[str],
        weather_hint: str | None,
        departure_date: date | None,
        candidates: list[WardrobeItem],
    ) -> PackingList:
        # Lazy import breaks the planner<->generate import cycle (planner imports
        # SCENE_CATEGORIES from this module at load time).
        from app.application.packing.planner import build_trip_plan, parse_weather_hint
        from app.application.recommendations.engine import RecommendationEngine
        from app.application.recommendations.generator import RecommendationGenerator
        from app.infrastructure.persistence.repositories.recommendation_repo import (
            SqlAlchemyRecommendationRepository,
        )
        from app.infrastructure.persistence.repositories.style_repo import SqlAlchemyStyleRepository
        from app.infrastructure.persistence.repositories.wardrobe_relationship_repo import (
            SqlAlchemyWardrobeRelationshipRepository,
        )

        session = self.repository.session
        settings = get_settings()
        # Reuse the recommendation generator's signal loaders (fail-soft) rather
        # than duplicating them — same application layer.
        gen = RecommendationGenerator(
            SqlAlchemyRecommendationRepository(session),
            style_repository=SqlAlchemyStyleRepository(session),
            engine=RecommendationEngine(),
        )
        disliked_colors = await gen._load_disliked_colors(user_id, settings)
        relationship_edges = await gen._load_relationship_edges(
            user_id, SqlAlchemyWardrobeRelationshipRepository(session), candidates, settings
        )
        style_profile = await gen._load_style_profile(user_id)
        fit_profile = await gen._load_fit_profile(user_id)

        plan = await build_trip_plan(
            candidate_items=candidates,
            scenes=scenes,
            days=days,
            weather=parse_weather_hint(weather_hint),
            engine=gen.engine,
            disliked_colors=disliked_colors,
            relationship_edges=relationship_edges,
            style_profile=style_profile,
            fit_profile=fit_profile,
            destination=destination,
        )
        # Flatten the per-day plan into the legacy checklist so `items` and the
        # rich `plan_json` describe the same selection.
        selected = [
            {
                "item_id": item_id,
                "day": daily.day,
                "scene": daily.occasion,
                "notes": "",
                "packed": False,
            }
            for daily in plan.daily_outfits
            for item_id in daily.item_ids
        ]
        packing_list = PackingList(
            user_id=user_id,
            destination=destination,
            days=days,
            scenes=list(scenes),
            departure_date=departure_date,
            items=selected,
            plan_json=plan.to_dict(),
            status="draft",
        )
        return await self.repository.add(packing_list)


class GetPackingList:
    def __init__(self, repository: SqlAlchemyPackingListRepository) -> None:
        self.repository = repository

    async def execute(
        self, packing_list_id: uuid.UUID, user_id: uuid.UUID
    ) -> PackingList:
        packing_list = await self.repository.get_for_user(packing_list_id, user_id)
        if packing_list is None:
            raise FriendlyException(
                error_codes.NOT_FOUND, "Packing list not found", status_code=404
            )
        return packing_list


class ListPackingLists:
    def __init__(self, repository: SqlAlchemyPackingListRepository) -> None:
        self.repository = repository

    async def execute(self, user_id: uuid.UUID) -> list[PackingList]:
        return await self.repository.list_for_user(user_id)


class UpdatePackingList:
    """User edits: replace the item selection and/or flip status (draft/confirmed)."""

    _ALLOWED_STATUSES = {"draft", "confirmed"}

    def __init__(self, repository: SqlAlchemyPackingListRepository) -> None:
        self.repository = repository

    async def execute(
        self,
        packing_list_id: uuid.UUID,
        user_id: uuid.UUID,
        items: list[dict[str, object]] | None,
        status: str | None,
    ) -> PackingList:
        if status is not None and status not in self._ALLOWED_STATUSES:
            raise FriendlyException(
                error_codes.VALIDATION_ERROR,
                "status must be one of: draft, confirmed",
                status_code=422,
            )
        packing_list = await self.repository.get_for_user(packing_list_id, user_id)
        if packing_list is None:
            raise FriendlyException(
                error_codes.NOT_FOUND, "Packing list not found", status_code=404
            )
        if items is not None:
            packing_list.items = items
            # BE#68 review: a user-edited checklist no longer matches the planner
            # snapshot, so drop the now-stale rich plan rather than serve an
            # inconsistent items-vs-plan response.
            packing_list.plan_json = None
        if status is not None:
            packing_list.status = status
        return await self.repository.update(packing_list)
