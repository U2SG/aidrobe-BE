"""M16 Travel Packing List use cases — rule-based, no LLM.

Generation walks days x scenes and picks wardrobe items per scene's expected
categories. It prefers status=wearable + fit_status=fits_well (the candidate
query already orders fits_well first) and skips items worn in the last 7 days
(via outfit wear history). The result is stored on `packing_lists.items` as a
JSONB array of {item_id, day, scene, notes}. Marking the list confirmed is a
plain status flip.
"""

import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from typing import TypedDict, cast

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ask.design_types import StylingConstraints
from app.application.ask.styling import StylingService, conflict, fingerprint, lock_request
from app.application.ports.weather import WeatherInfo, WeatherPort
from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.common.item_labels import localized_item_name
from app.common.locale import DEFAULT_LOCALE
from app.common.season import current_season, in_season, season_tags_of
from app.config.settings import get_settings
from app.infrastructure.persistence.models.ask_conversation import AskStylingRevision
from app.infrastructure.persistence.models.packing_list import PackingList
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.packing_list_repo import (
    SqlAlchemyPackingListRepository,
)

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
_REQUEST_FINGERPRINT = "_request_fingerprint"


def _categories_for_scene(scene: str) -> list[str]:
    key = scene.lower()
    key = SCENE_ALIASES.get(key, key)
    categories = SCENE_CATEGORIES.get(key)
    if categories is None:
        logger.info("packing.unknown_scene", scene=scene)
        return DEFAULT_CATEGORIES
    return categories


@dataclass(frozen=True)
class _DesignSource:
    outfit_revision: AskStylingRevision
    expected_revision: int


@dataclass(frozen=True)
class _PackingRequest:
    packing_list_id: uuid.UUID
    fingerprint: str


def _design_trip_day(scene: str | None, scenes: list[str], days: int) -> int:
    if scene is None:
        return 1
    design_scene = SCENE_ALIASES.get(scene, scene)
    for day, trip_scene in enumerate(scenes[:days], start=1):
        key = trip_scene.lower()
        if SCENE_ALIASES.get(key, key) == design_scene:
            return day
    raise FriendlyException(
        error_codes.VALIDATION_ERROR, "Include a trip occasion that matches the saved outfit.", 422,
        data={"reason": "packing_scene_mismatch"},
    )


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
        conversation_id: uuid.UUID | None = None,
        expected_revision: int | None = None,
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

        if conversation_id is None and (expected_revision is not None or request_id is not None):
            raise FriendlyException(error_codes.VALIDATION_ERROR, "Provide a design conversation", 422)

        request = None
        if request_id is not None:
            request = _PackingRequest(
                uuid.uuid5(uuid.NAMESPACE_URL, f"aidrobe:packing-list:{user_id}:{request_id}"),
                fingerprint(
                    "design-packing", conversation_id=conversation_id, expected_revision=expected_revision,
                    destination=destination, days=days, scenes=scenes, weather_hint=weather_hint,
                    departure_date=departure_date,
                ),
            )
            await lock_request(self.repository.session, user_id, request.packing_list_id)
            existing = await self.repository.get_for_user(request.packing_list_id, user_id)
            if existing is not None:
                if (existing.plan_json or {}).get(_REQUEST_FINGERPRINT) != request.fingerprint:
                    raise FriendlyException(
                        error_codes.STATE_CONFLICT, "request_id was already used for a different packing list", 409,
                        data={"reason": "packing_request_conflict"},
                    )
                return existing

        source = await self._resolve_design_source(user_id, conversation_id, expected_revision)
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
                user_id, destination, days, scenes, weather_hint, departure_date, candidates, locale, source, request,
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

    async def _resolve_design_source(
        self, user_id: uuid.UUID, conversation_id: uuid.UUID | None, expected_revision: int | None,
    ) -> _DesignSource | None:
        if conversation_id is None:
            return None
        settings = get_settings()
        if not settings.ENABLE_DESIGNER_SESSIONS or not settings.ENABLE_PACKING_PLANNER:
            raise FriendlyException(error_codes.FORBIDDEN, "Design trip planning is not enabled", 503)
        service = StylingService(self.repository.session, user_id)
        conversation = await service.conversations.get_owned_conversation(user_id, conversation_id)
        if conversation is None:
            raise FriendlyException(error_codes.NOT_FOUND, "Conversation not found", 404)
        if expected_revision is not None and expected_revision != conversation.styling_revision:
            raise conflict()
        current = await service.current(conversation.id)
        if current is None:
            raise conflict("Save an outfit version before planning this trip.")
        service.require_current_outfit(conversation, current)
        return _DesignSource(current, conversation.styling_revision)

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
        source: _DesignSource | None,
        request: _PackingRequest | None,
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
        daily_weather = await self._destination_forecast(destination, departure_date, days)
        saved_outfit: tuple[int, list[str]] | None = None
        if source is not None:
            revision = source.outfit_revision
            service = StylingService(session, user_id)
            # Recheck after the forecast call so an outfit changed while this
            # trip was opening cannot attach an outdated revision silently.
            conversation = await service.lock_conversation(revision.conversation_id, source.expected_revision)
            service.require_current_outfit(conversation, revision)
            outfit = await service.owned_outfit(revision.outfit_id)
            constraints = StylingConstraints.model_validate(revision.constraints)
            design_day = _design_trip_day(constraints.scene, scenes, days)
            design_items = await service.validate_items(
                [item.item_id for item in sorted(outfit.items, key=lambda row: row.display_order)], constraints,
            )
            travel_date = (departure_date or date.today()) + timedelta(days=design_day - 1)
            trip_season = current_season(travel_date)
            if any(not in_season(season_tags_of(item.season_tags), trip_season) for item in design_items):
                raise FriendlyException(
                    error_codes.VALIDATION_ERROR, "Update the saved outfit for the trip's season.", 422,
                    data={"reason": "packing_season_mismatch"},
                )
            design_ids = {item.id for item in design_items}
            excluded_ids = set(constraints.excluded_item_ids)
            candidates = [
                item for item in candidates if item.id not in excluded_ids and item.id not in design_ids
            ] + design_items
            saved_outfit = (design_day, [str(item.id) for item in design_items])

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
            daily_weather=daily_weather,
            engine=gen.engine,
            disliked_colors=disliked_colors,
            relationship_edges=relationship_edges,
            style_profile=style_profile,
            fit_profile=fit_profile,
            destination=destination,
            locale=locale,
            saved_outfit=saved_outfit,
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
        plan_json = plan.to_dict()
        if request is not None:
            plan_json[_REQUEST_FINGERPRINT] = request.fingerprint
        packing_list = PackingList(
            id=request.packing_list_id if request is not None else uuid.uuid4(),
            user_id=user_id,
            conversation_id=source.outfit_revision.conversation_id if source else None,
            styling_revision_id=source.outfit_revision.id if source else None,
            destination=destination,
            days=days,
            scenes=list(scenes),
            departure_date=departure_date,
            items=selected,
            plan_json=plan_json,
            status="draft",
        )
        return await self.repository.add(packing_list)


async def load_conversation_packing_list(
    session: AsyncSession, user_id: uuid.UUID, conversation_id: uuid.UUID,
) -> PackingList | None:
    return await SqlAlchemyPackingListRepository(session).latest_for_conversation(user_id, conversation_id)


class PackedItemDescription(TypedDict):
    item_id: str
    name: str | None
    category: str | None


class PackingDayDescription(TypedDict):
    day: int
    items: list[PackedItemDescription]


class PackingListDescription(TypedDict):
    packing_list_id: str
    destination: str | None
    days: int
    daily_outfits: list[PackingDayDescription]


async def describe_packing_list(
    session: AsyncSession, packing_list: PackingList, locale: str = DEFAULT_LOCALE,
) -> PackingListDescription:
    """Read the saved selection, including edits made after plan generation."""
    by_day: dict[int, list[uuid.UUID]] = {}
    for row in cast(list[dict[str, object]], packing_list.items or []):
        day = cast(int, row["day"])
        item_id = uuid.UUID(str(row["item_id"]))
        day_pieces = by_day.setdefault(day, [])
        if item_id not in day_pieces:
            day_pieces.append(item_id)

    item_ids = {item_id for pieces in by_day.values() for item_id in pieces}
    items: list[WardrobeItem] = list(await session.scalars(select(WardrobeItem).where(
        WardrobeItem.user_id == packing_list.user_id,
        WardrobeItem.id.in_(item_ids),
        WardrobeItem.deleted_at.is_(None),
    ))) if item_ids else []
    descriptions: dict[uuid.UUID, PackedItemDescription] = {
        item.id: {
            "item_id": str(item.id),
            "category": item.category,
            "name": localized_item_name(
                title=item.title, color_main=item.color_main, category=item.category, locale=locale,
                localized_names=item.localized_names, name_source=item.name_source,
            ),
        }
        for item in items
    }
    daily_outfits: list[PackingDayDescription] = []
    for day, pieces in sorted(by_day.items()):
        day_items = [
            descriptions.get(item_id, {"item_id": str(item_id), "name": None, "category": None})
            for item_id in pieces
        ]
        daily_outfits.append({"day": day, "items": day_items})
    return {
        "packing_list_id": str(packing_list.id),
        "destination": packing_list.destination,
        "days": packing_list.days,
        "daily_outfits": daily_outfits,
    }


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
                request_fingerprint = (packing_list.plan_json or {}).get(_REQUEST_FINGERPRINT)
                packing_list.plan_json = (
                    {_REQUEST_FINGERPRINT: request_fingerprint} if request_fingerprint is not None else None
                )
            packing_list.items = items
        if status is not None:
            packing_list.status = status
        return await self.repository.update(packing_list)
