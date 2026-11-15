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
from typing import cast

import structlog
from sqlalchemy import select

from app.application.ask.continuity import (
    load_design_context,
    preferred_item_ids,
    record_linked_result,
    require_design_context_enabled,
    restrict_design_items,
)
from app.application.ask.continuity_types import DesignContextRef, DesignContextSnapshot, LinkedDesignResult
from app.application.ask.styling import conflict, fingerprint, lock_request
from app.application.ports.weather import WeatherInfo, WeatherPort
from app.application.preferences.memory import resolve_effective_preferences
from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.common.locale import DEFAULT_LOCALE
from app.config.settings import get_settings
from app.infrastructure.persistence.models.packing_list import PackingList
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.packing_list_repo import (
    SqlAlchemyPackingListRepository,
)
from app.infrastructure.persistence.repositories.preference_memory_repo import SqlAlchemyPreferenceMemoryRepository

logger = structlog.get_logger(__name__)

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
# Scene keys the client sends that map onto a generator scene.
SCENE_ALIASES: dict[str, str] = {
    "commute": "business",
    "travel": "casual",
    "date": "dinner",
    "leisure": "casual",
}
# Fallback when a scene is not recognised.
DEFAULT_CATEGORIES: list[str] = ["top", "bottom", "shoes"]

RECENT_WEAR_DAYS = 7


def _categories_for_scene(scene: str) -> list[str]:
    key = scene.lower()
    key = SCENE_ALIASES.get(key, key)
    categories = SCENE_CATEGORIES.get(key)
    if categories is None:
        logger.info("packing.unknown_scene", scene=scene)
        return DEFAULT_CATEGORIES
    return categories


class GeneratePackingList:
    def __init__(
        self,
        repository: SqlAlchemyPackingListRepository,
        weather: WeatherPort | None = None,
    ) -> None:
        self.repository = repository
        self.weather = weather

    async def execute(
        self,
        user_id: uuid.UUID,
        destination: str | None,
        days: int,
        scenes: list[str],
        weather_hint: str | None,
        departure_date: date | None,
        locale: str = DEFAULT_LOCALE,
        *,
        design_context: DesignContextRef | None = None,
        request_id: uuid.UUID | None = None,
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

        if (design_context is None) != (request_id is None):
            raise FriendlyException(error_codes.VALIDATION_ERROR,
                                    "design_context and request_id must be provided together", status_code=422)
        context: DesignContextSnapshot | None = None
        request_fingerprint: str | None = None
        if design_context is not None and request_id is not None:
            require_design_context_enabled()
            request_fingerprint = fingerprint(
                "packing_design", destination=destination, days=days, scenes=scenes,
                weather_hint=weather_hint, departure_date=departure_date,
                design_context=design_context.model_dump(mode="json"),
            )
            await lock_request(self.repository.session, user_id, request_id)
            existing = await self.repository.session.scalar(select(PackingList).where(
                PackingList.user_id == user_id, PackingList.design_request_id == request_id,
            ))
            if existing is not None:
                if existing.design_request_fingerprint != request_fingerprint:
                    raise conflict("request_id was already used for a different travel plan")
                return existing
            context = await load_design_context(self.repository.session, user_id, design_context)

        candidates = await self.repository.list_candidate_items(user_id)

        # Skip items worn in the 7 days before departure — anchored to the trip,
        # not today, so items packed for a future trip are still available now.
        # Applied to BOTH paths (BE#68 review): the planner must also respect DB
        # wear history, not only the looks it plans within this trip.
        anchor = departure_date if departure_date is not None else date.today()
        since = anchor - timedelta(days=RECENT_WEAR_DAYS)
        recently_worn = await self.repository.recent_worn_item_ids(user_id, since, until=anchor)
        locked = set(context.constraints.locked_item_ids) if context else set()
        candidates = [item for item in candidates if item.id not in recently_worn or item.id in locked]
        if context is not None:
            candidates = restrict_design_items(
                candidates, context, preferred=await preferred_item_ids(self.repository.session, context),
            )

        # BE#68 — when the planner is on, build the rich per-day plan (engine +
        # preferences + graph), persist it, and derive the flat checklist from it
        # so both views stay consistent. Flag off → the legacy round-robin below.
        if get_settings().ENABLE_PACKING_PLANNER:
            result = await self._generate_with_planner(
                user_id, destination, days, scenes, weather_hint, departure_date, candidates, locale, context,
            )
            return await self._save_context(result, context, request_id, request_fingerprint, locale)

        by_category: dict[str, list[WardrobeItem]] = {}
        for item in candidates:
            by_category.setdefault(item.category.lower(), []).append(item)

        # Round-robin a pointer per category so successive days rotate items
        # rather than repeating the same piece every day.
        cursor: dict[str, int] = {}
        selected: list[dict[str, object]] = []
        for day in range(1, days + 1):
            for scene in scenes:
                categories = list(_categories_for_scene(scene))
                if context is not None:
                    categories = list(dict.fromkeys([*categories, *(item.category.lower() for item in candidates
                                                                  if item.id in locked)]))
                for category in categories:
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
        result = await self.repository.add(packing_list)
        return await self._save_context(result, context, request_id, request_fingerprint, locale)

    async def _save_context(
        self, packing_list: PackingList, context: DesignContextSnapshot | None,
        request_id: uuid.UUID | None, request_fingerprint: str | None, locale: str,
    ) -> PackingList:
        if context is None:
            return packing_list
        packing_list.design_context = context.model_dump(mode="json")
        packing_list.design_request_id = request_id
        packing_list.design_request_fingerprint = request_fingerprint
        await self.repository.session.flush()
        items = cast(list[dict[str, object]], packing_list.items or [])
        count = len({str(item["item_id"]) for item in items})
        destination = packing_list.destination or ("your trip" if locale.startswith("en") else "这次旅行")
        summary = (
            f"Prepared a {packing_list.days}-day packing list for {destination}, using {count} wardrobe items."
            if locale.startswith("en")
            else f"已为{destination}准备 {packing_list.days} 天旅行清单，共选用 {count} 件衣物。"
        )
        await record_linked_result(self.repository.session, packing_list.user_id, context, LinkedDesignResult(
            kind="packing_list", resource_id=packing_list.id, source_revision=context.revision, summary=summary[:1000],
        ))
        return packing_list

    async def _destination_forecast(
        self,
        destination: str | None,
        departure_date: date | None,
        days: int,
    ) -> list[WeatherInfo | None] | None:
        """BE#118 — real per-day forecasts for the destination. Needs a weather
        port plus both destination and departure date; every failure degrades to
        None for that day (planner falls back to the parsed weather hint), so a
        provider outage can never block list creation."""
        if self.weather is None or not destination or departure_date is None:
            return None
        forecast: list[WeatherInfo | None] = []
        for offset in range(days):
            target = departure_date + timedelta(days=offset)
            try:
                forecast.append(await self.weather.get_forecast(destination, target))
            except Exception as exc:
                logger.warning(
                    "packing_forecast_failed",
                    destination=destination,
                    target_date=str(target),
                    error=str(exc),
                )
                forecast.append(None)
        return forecast if any(w is not None for w in forecast) else None

    async def _generate_with_planner(
        self,
        user_id: uuid.UUID,
        destination: str | None,
        days: int,
        scenes: list[str],
        weather_hint: str | None,
        departure_date: date | None,
        candidates: list[WardrobeItem],
        locale: str,
        context: DesignContextSnapshot | None = None,
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
        daily_disliked_colors: list[set[str]] | None = None
        if context is not None and settings.ENABLE_PREFERENCE_MEMORY:
            rows = await SqlAlchemyPreferenceMemoryRepository(session).list_live(user_id)
            daily_disliked_colors = []
            for day in range(days):
                target = departure_date + timedelta(days=day) if departure_date is not None else None
                scene = scenes[day % len(scenes)]
                overrides = context.constraints.preference_overrides if (
                    target == context.constraints.target_date and scene == context.constraints.scene
                ) else []
                preferences = resolve_effective_preferences(
                    rows, scene=scene, target_date=target,
                    overrides=[override.model_dump(mode="json", exclude_none=True) for override in overrides],
                )
                daily_disliked_colors.append({row.preference_value.lower() for row in preferences
                                              if row.polarity == "negative" and row.preference_type == "color"})
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
            daily_weather=await self._destination_forecast(destination, departure_date, days),
            engine=gen.engine,
            disliked_colors=disliked_colors,
            relationship_edges=relationship_edges,
            style_profile=style_profile,
            fit_profile=fit_profile,
            destination=destination,
            locale=locale,
            required_item_ids={str(item_id) for item_id in context.constraints.locked_item_ids} if context else None,
            daily_disliked_colors=daily_disliked_colors,
            design_context=context,
            departure_date=departure_date,
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


def _selection(rows: list[dict[str, object]]) -> set[tuple[str, object, object]]:
    return {(str(r["item_id"]), r["day"], r["scene"]) for r in rows}


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
            # BE#68 review: a user-edited checklist no longer matches the planner
            # snapshot, so drop the now-stale rich plan rather than serve an
            # inconsistent items-vs-plan response. Only the planned selection
            # counts: toggling `packed` / editing `notes` keeps the plan.
            if _selection(items) != _selection(cast(list[dict[str, object]], packing_list.items or [])):
                packing_list.plan_json = None
            packing_list.items = items
        if status is not None:
            packing_list.status = status
        return await self.repository.update(packing_list)
