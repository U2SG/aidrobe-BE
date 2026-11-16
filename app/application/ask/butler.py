"""Read-only wardrobe tools with turn-local item positions and outfit approvals."""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from itertools import product
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from sqlalchemy import Select, func, nulls_last, or_, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.application.ask.design_types import PreferenceOverride, StylingConstraints
from app.application.ask.tools import AskTool, ToolRegistry
from app.application.ports.llm import ToolResult, ToolSpec
from app.application.ports.weather import WeatherInfo, WeatherPort
from app.application.preferences.memory import read_preference_memory
from app.application.recommendations.engine import BUSINESS_FORMALITY, CASUAL_FORMALITY, COLD_FEELS_LIKE_C
from app.application.recommendations.feasibility import has_first_outfit_core, missing_first_outfit_slots
from app.common.enums import Category, FitScene, FitStatus, WardrobeItemStatus
from app.common.season import SeasonName, current_season, in_season, season_tags_of
from app.infrastructure.llm.sanitizer import sanitize_for_llm, sanitize_llm_text
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.preference_memory_repo import SqlAlchemyPreferenceMemoryRepository

_CORE_CATEGORIES = frozenset(category.value for category in Category)


class _ToolArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class _WeatherArgs(_ToolArgs):
    when: str = Field(default="today", max_length=10, description="now, today, tomorrow, or YYYY-MM-DD")
    place: str | None = Field(default=None, min_length=1, max_length=120, description="A city named by the user")


class _SearchArgs(_ToolArgs):
    category: Annotated[str, StringConstraints(min_length=1, max_length=32)] | None = None
    color: str | None = Field(default=None, min_length=1, max_length=32)
    season: SeasonName | None = None
    min_formality: int | None = Field(default=None, ge=0, le=100)
    max_formality: int | None = Field(default=None, ge=0, le=100)
    status: WardrobeItemStatus = WardrobeItemStatus.WEARABLE
    fit_status: FitStatus | None = None
    text: str | None = Field(default=None, min_length=1, max_length=120, description="Literal title or garment type")
    style: str | None = Field(default=None, max_length=80, description="Desired style tag, if stated by the user")
    limit: int = Field(default=12, ge=1, le=20)


class _OutfitArgs(_WeatherArgs):
    positions: list[Annotated[int, Field(strict=True, ge=1)]] = Field(min_length=2, max_length=8)
    occasion: FitScene | None = None


class _BriefArgs(_ToolArgs):
    evidence_text: str = Field(min_length=1, max_length=600, description="Exact quote from the current user message")
    goal: str | None = Field(default=None, max_length=600)
    target_style: str | None = Field(default=None, max_length=120)
    exploration: bool | None = None
    notes: list[str] | None = Field(default=None, max_length=8)
    locked_positions: list[Annotated[int, Field(strict=True, ge=1)]] | None = Field(default=None, max_length=8)
    excluded_positions: list[Annotated[int, Field(strict=True, ge=1)]] | None = Field(default=None, max_length=100)
    change_positions: list[Annotated[int, Field(strict=True, ge=1)]] = Field(
        default_factory=list, max_length=8,
        description="Existing outfit pieces the user wants to change; retain all others",
    )
    redesign_all: bool = Field(
        default=False, description="True only for an explicit request to redesign the whole look"
    )
    preference_overrides: list[PreferenceOverride] | None = Field(default=None, max_length=8)
    when: str | None = None
    scene: str | None = Field(default=None, max_length=32)


@dataclass(frozen=True)
class _CheckedOutfit:
    args: _OutfitArgs
    target: date



def _spec(name: str, description: str, args: type[_ToolArgs]) -> ToolSpec:
    return ToolSpec(name=name, description=description, parameters=args.model_json_schema())


def _positions(raw: object, size: int, *, limit: int = 20) -> list[int] | None:
    if not isinstance(raw, list) or len(raw) > limit:
        return None
    if any(type(pos) is not int or not 1 <= pos <= size for pos in raw):
        return None
    if len(set(raw)) != len(raw):
        return None
    return list(raw)


def _args_date(when: str, today: date) -> date | None:
    if when in {"now", "today"}:
        return today
    if when == "tomorrow":
        return today + timedelta(days=1)
    try:
        day = date.fromisoformat(when)
    except ValueError:
        return None
    return day if day.isoformat() == when else None


def _outfit_issues(
    items: list[WardrobeItem], target: date, occasion: FitScene | None, weather: WeatherInfo | None
) -> dict[str, Any]:
    categories = [item.category for item in items]
    missing = missing_first_outfit_slots(categories)
    if missing:
        return {"missing_categories": missing}
    core = [category for category in categories if category in _CORE_CATEGORIES]
    if len(set(core)) < len(core) or ("dress" in core and {"top", "bottom"}.intersection(core)):
        return {"error": "conflicting outfit slots"}
    if any(not in_season(season_tags_of(item.season_tags), current_season(target)) for item in items):
        return {"error": "includes out-of-season pieces"}
    if weather is not None and weather.feels_like_c < COLD_FEELS_LIKE_C and "outerwear" not in categories:
        return {"missing_categories": ["outerwear"]}
    scores = [item.formality_score for item in items if item.formality_score is not None]
    if scores:
        mean = sum(scores) / len(scores)
        if occasion == FitScene.FORMAL and mean < BUSINESS_FORMALITY:
            return {"error": "not formal enough"}
        if occasion == FitScene.CASUAL and mean > CASUAL_FORMALITY:
            return {"error": "too formal"}
    return {}


def pick_inspiration_outfit(
    items: list[WardrobeItem], constraints: StylingConstraints, *, target: date,
    weather: WeatherInfo | None, palette: set[str], styles: set[str],
) -> list[WardrobeItem]:
    """Rank complete wardrobe looks by the saved photo's visual signals."""
    required = set(constraints.locked_item_ids)
    excluded = set(constraints.excluded_item_ids)
    buckets: dict[str, list[WardrobeItem]] = {category: [] for category in _CORE_CATEGORIES}
    season = current_season(target)
    for item in items:
        if (
            item.category in buckets and item.id not in excluded and item.status == WardrobeItemStatus.WEARABLE
            and item.fit_status != FitStatus.NO_LONGER_FITS and in_season(season_tags_of(item.season_tags), season)
        ):
            buckets[item.category].append(item)
    for category, choices in buckets.items():
        locked = [item for item in choices if item.id in required]
        if locked:
            buckets[category] = locked

    def style_matches(item: WardrobeItem) -> int:
        raw = (item.style_tags or {}).get("tags")
        tags = {tag.lower() for tag in raw if isinstance(tag, str)} if isinstance(raw, list) else set()
        return len(tags & styles)

    matches = {item.id: style_matches(item) for item in items}
    outerwear = buckets[Category.OUTERWEAR]
    layers: list[WardrobeItem | None] = [*outerwear, None]
    occasion = FitScene(constraints.scene) if constraints.scene else None
    best: list[WardrobeItem] = []
    best_score = (-1, -1, -1, -1)
    for core in ((Category.TOP, Category.BOTTOM, Category.SHOES), (Category.DRESS, Category.SHOES)):
        for combo in product(*(buckets[category] for category in core), layers):
            look = [item for item in combo if item is not None]
            if not required.issubset(item.id for item in look) or _outfit_issues(look, target, occasion, weather):
                continue
            colors = [item.color_main.lower() for item in look]
            score = (
                len(set(colors) & palette), sum(color in palette for color in colors),
                sum(matches[item.id] for item in look), -len(look),
            )
            if score > best_score:
                best = look
                best_score = score
    return best


def _search_fields(item: WardrobeItem) -> dict[str, Any]:
    # Text search may match a user title, but the title and storage location
    # never leave the server. Model-visible item attributes stay bounded.
    fields = sanitize_for_llm({
        "category": item.category,
        "sub_category": item.sub_category,
        "color_main": item.color_main,
        "color_secondary": item.color_secondary,
        "material": item.material,
        "status": item.status,
        "fit_status": item.fit_status,
        "formality_score": item.formality_score,
        "season_tags": [
            tag for tag in season_tags_of(item.season_tags)
            if tag in {"spring", "summer", "autumn", "winter", "all_season"}
        ],
    })
    return {
        key: sanitize_llm_text(value, max_len=64) if isinstance(value, str) else value
        for key, value in fields.items()
        if value is not None
    } | {"style_tags": [sanitize_llm_text(str(tag), max_len=50)
                       for tag in (item.style_tags or {}).get("tags", [])[:8]]}


class AskButler:
    def __init__(
        self,
        session: AsyncSession,
        user_id: uuid.UUID,
        items: list[WardrobeItem],
        *,
        today: date,
        location: str | None,
        weather: WeatherPort | None,
        lang: str,
        focused_item_id: uuid.UUID | None,
        excluded_ids: set[str],
        context: Mapping[str, object] | None,
        design_constraints: StylingConstraints | None = None,
        current_outfit_ids: list[uuid.UUID] | None = None,
        question: str = "",
        preference_memory_enabled: bool = False,
    ) -> None:
        self.session = session
        self.user_id = user_id
        self.today = today
        self.location = location
        self.weather = weather
        self.lang = lang
        self.focused_item_id = focused_item_id
        self.design_constraints = design_constraints
        self.current_outfit_ids = set(current_outfit_ids or [])
        self.preserved_ids = set(self.current_outfit_ids)
        self.question = question
        self.preference_memory_enabled = preference_memory_enabled
        self.effective_preferences: dict[str, list[dict[str, Any]]] = {}
        self.brief_updated = False
        self.excluded_ids: set[uuid.UUID] = set()
        for item_id in excluded_ids:
            try:
                self.excluded_ids.add(uuid.UUID(item_id))
            except ValueError:
                continue
        if focused_item_id is not None and (
            design_constraints is None or focused_item_id not in design_constraints.excluded_item_ids
        ):
            self.excluded_ids.discard(focused_item_id)
        raw_date = (context or {}).get("date")
        self.requested_date = _args_date(raw_date, today) if isinstance(raw_date, str) else None
        if self.requested_date is not None and self.requested_date < today:
            self.requested_date = None
        raw_occasion = (context or {}).get("scene")
        self.requested_occasion = next((scene for scene in FitScene if scene.value == raw_occasion), None)
        if design_constraints is not None:
            self.excluded_ids.update(design_constraints.excluded_item_ids)
            self.requested_date = design_constraints.target_date or self.requested_date
            self.requested_occasion = next(
                (scene for scene in FitScene if scene.value == design_constraints.scene), self.requested_occasion
            )
        self.item_ids = [item.id for item in items]
        self._known_positions = {item_id: pos for pos, item_id in enumerate(self.item_ids, start=1)}
        self._weather: dict[tuple[date, bool, str], tuple[WeatherInfo, bool] | None] = {}
        self._checked: dict[frozenset[int], _CheckedOutfit] = {}
        tools = [
            AskTool(
                spec=_spec(
                    "get_weather", "Read weather for the requested date or current conditions.", _WeatherArgs
                ),
                handler=self.get_weather,
            ),
            AskTool(
                spec=_spec(
                    "search_wardrobe",
                    "Search owned pieces by category, colour, title, season, formality or status. "
                    "Results use the same 1-based positions as the initial wardrobe list. "
                    "Search by title to identify a particular piece or check why it cannot be worn.",
                    _SearchArgs,
                ),
                handler=self.search_wardrobe,
            ),
            AskTool(
                spec=_spec(
                    "validate_outfit",
                    "Check the exact proposed set before recommending it. Include top + bottom or a dress, and shoes. "
                    "Use the target date, the requested city, and the occasion (formal for an interview or meeting).",
                    _OutfitArgs,
                ),
                handler=self.validate_outfit,
            ),
        ]
        if design_constraints is not None:
            tools.insert(0, AskTool(
                spec=_spec("update_design_brief", "Apply this turn's explicit goal and temporary exceptions first. "
                           "When changing only certain pieces, list change_positions; all other current pieces stay. "
                           "This never writes long-term memory. Quote only the CURRENT user message.", _BriefArgs),
                handler=self.update_design_brief,
            ))
        self.registry = ToolRegistry(tools)

    @property
    def prompt_context(self) -> str:
        details = f"\nLocal date: {self.today.isoformat()}."
        if self.requested_date is not None:
            details += f" Requested outfit date: {self.requested_date.isoformat()}."
        if self.requested_occasion is not None:
            details += f" Requested occasion: {self.requested_occasion.value}."
        if self.design_constraints is not None:
            brief = self.design_constraints.model_dump(mode="json", exclude={
                "inspiration_id", "locked_item_ids", "excluded_item_ids", "preference_overrides"
            })
            brief["goal"] = sanitize_llm_text(self.design_constraints.goal or "", max_len=600)
            brief["target_style"] = sanitize_llm_text(self.design_constraints.target_style or "", max_len=120)
            brief["notes"] = [sanitize_llm_text(note, max_len=240) for note in self.design_constraints.notes]
            brief["current_positions"] = [self._known_positions[ident] for ident in self.current_outfit_ids
                                          if ident in self._known_positions]
            brief["locked_positions"] = [
                self._known_positions[ident] for ident in self.design_constraints.locked_item_ids
                if ident in self._known_positions
            ]
            details += "\nDesign session: " + json.dumps(brief, ensure_ascii=False)
            details += ("\nCall update_design_brief before searching or validating. Use a quote from the current user "
                        "message. Preserve existing pieces except change_positions; never change a locked piece. "
                        "Exploration describes a temporary experiment, not a durable preference. A style request "
                        "must shape the actual garment attributes and selection. If the wardrobe cannot achieve "
                        "it, explain the concrete gap. New complete results are drafts; do not claim adoption, "
                        "wear, favorite or image generation. Users adopt with the visible Use this outfit action.")
        return details

    async def update_design_brief(self, raw: dict[str, Any]) -> ToolResult:
        if self.design_constraints is None:
            return ToolResult({"error": "no design session"}, "")
        try:
            args = _BriefArgs.model_validate(raw)
            if args.evidence_text not in self.question:
                return ToolResult({"error": "evidence must be an exact quote from the current message"}, "")
            changes = _positions(args.change_positions, len(self.item_ids), limit=8)
            if changes is None or not {self.item_ids[pos - 1] for pos in changes}.issubset(self.current_outfit_ids):
                return ToolResult({"error": "change_positions must identify current outfit pieces"}, "")
            values = self.design_constraints.model_dump()
            for name in ("goal", "target_style", "exploration", "notes", "scene"):
                value = getattr(args, name)
                if value is not None:
                    values[name] = value
            for name in ("locked", "excluded"):
                raw_positions = getattr(args, f"{name}_positions")
                if raw_positions is not None:
                    positions = _positions(raw_positions, len(self.item_ids), limit=100)
                    if positions is None:
                        return ToolResult({"error": "unknown item position"}, "")
                    values[f"{name}_item_ids"] = [self.item_ids[pos - 1] for pos in positions]
            if args.preference_overrides is not None:
                values["preference_overrides"] = [entry.model_dump() for entry in args.preference_overrides]
            if args.when is not None:
                target = _args_date(args.when, self.today)
                if target is None or target < self.today:
                    return ToolResult({"error": "use today or a future date"}, "")
                values["target_date"] = target
            values["target_date"] = values.get("target_date") or self.today
            if args.preference_overrides is None and (
                values["target_date"] != self.design_constraints.target_date
                or values.get("scene") != self.design_constraints.scene
            ):
                values["preference_overrides"] = []
            candidate = StylingConstraints.model_validate(values)
        except ValidationError:
            return ToolResult({"error": "invalid design brief"}, "")
        if self.preference_memory_enabled:
            async with self._tool_read_session() as tool_session:
                preferences = await read_preference_memory(
                    SqlAlchemyPreferenceMemoryRepository(tool_session), self.user_id,
                    scene=candidate.scene, target_date=candidate.target_date,
                    overrides=[
                        entry.model_dump(mode="json", exclude_none=True) for entry in candidate.preference_overrides
                    ],
                )
            self.effective_preferences = preferences
        self.design_constraints = candidate
        self.preserved_ids = set() if args.redesign_all else self.current_outfit_ids.difference(
            self.item_ids[pos - 1] for pos in changes
        )
        self.excluded_ids = set(candidate.excluded_item_ids)
        self.requested_date = candidate.target_date
        self.requested_occasion = next((scene for scene in FitScene if scene.value == candidate.scene), None)
        self._checked.clear()
        self.brief_updated = True
        return ToolResult({"updated": True, "preferences": self.effective_preferences,
                           "retained_positions": [self._known_positions[ident] for ident in self.preserved_ids
                                                  if ident in self._known_positions]},
                          "Updated your design brief" if self.lang == "en" else "更新了本次穿搭目标")

    def _tool_read_session(self) -> AsyncSession:
        # A provider's total deadline may cancel a DB query. Use an independent
        # connection so asyncpg cancellation cannot poison the request transaction
        # needed by final verification, fallback and persistence.
        bind = self.session.bind
        assert bind is not None
        return AsyncSession(bind=bind.engine if isinstance(bind, AsyncConnection) else bind, expire_on_commit=False)

    def _owned_items(self) -> Select[tuple[WardrobeItem]]:
        return select(WardrobeItem).where(
            WardrobeItem.user_id == self.user_id,
            WardrobeItem.deleted_at.is_(None),
            WardrobeItem.id.not_in(self.excluded_ids),
        )

    async def search_wardrobe(self, raw: dict[str, Any]) -> ToolResult:
        if self.design_constraints is not None and not self.brief_updated:
            return ToolResult({"error": "update_design_brief first"}, "")
        try:
            args = _SearchArgs.model_validate(raw)
        except ValidationError:
            return ToolResult({"error": "invalid search filters"}, "")
        query = self._owned_items().where(WardrobeItem.status == args.status.value)
        if args.fit_status is not None:
            query = query.where(WardrobeItem.fit_status == args.fit_status.value)
        elif args.status == WardrobeItemStatus.WEARABLE:
            query = query.where(WardrobeItem.fit_status != FitStatus.NO_LONGER_FITS.value)
        if args.category is not None:
            query = query.where(func.lower(WardrobeItem.category) == args.category.lower())
        if args.color:
            query = query.where(func.lower(WardrobeItem.color_main) == args.color.lower())
        if args.season:
            query = query.where(or_(
                WardrobeItem.season_tags.is_(None),
                WardrobeItem.season_tags["tags"].is_(None),
                WardrobeItem.season_tags["tags"] == [],
                WardrobeItem.season_tags.contains({"tags": [args.season]}),
                WardrobeItem.season_tags.contains({"tags": ["all_season"]}),
            ))
        if (
            args.min_formality is not None
            and args.max_formality is not None
            and args.min_formality > args.max_formality
        ):
            return ToolResult({"error": "invalid formality range"}, "")
        if args.min_formality is not None:
            query = query.where(WardrobeItem.formality_score >= args.min_formality)
        if args.max_formality is not None:
            query = query.where(WardrobeItem.formality_score <= args.max_formality)
        if args.text:
            term = args.text
            query = query.where(or_(
                WardrobeItem.title.icontains(term, autoescape=True),
                WardrobeItem.canonical_name.icontains(term, autoescape=True),
                WardrobeItem.sub_category.icontains(term, autoescape=True),
            ))
        if args.style:
            query = query.where(WardrobeItem.style_tags.contains({"tags": [args.style]}))
        exploring = self.design_constraints is not None and self.design_constraints.exploration
        async with self._tool_read_session() as tool_session:
            rows = list(await tool_session.scalars(
                query.order_by(
                    WardrobeItem.wear_count.asc() if exploring else WardrobeItem.wear_count.desc(),
                    nulls_last(WardrobeItem.last_worn_at.desc()), WardrobeItem.id
                ).limit(80 if self.design_constraints is not None else args.limit)
            ))
        if self.design_constraints is not None:
            attributes = {
                "color": "color_main", "material": "material", "fit": "fit_status", "footwear": "sub_category"
            }
            negatives = self.effective_preferences.get("negative", [])
            desired_style = (self.design_constraints.target_style or "").casefold()

            def rank(item: WardrobeItem) -> tuple[int, int]:
                penalty = sum(1 for pref in negatives if attributes.get(str(pref.get("type")))
                              and str(getattr(item, attributes[str(pref["type"])], "")).casefold()
                              == str(pref.get("value", "")).casefold())
                tags = [str(tag).casefold() for tag in (item.style_tags or {}).get("tags", [])]
                return penalty, -int(bool(desired_style) and desired_style in tags)

            rows.sort(key=rank)
            rows = rows[:args.limit]
        results: list[dict[str, Any]] = []
        for item in rows:
            position = self._known_positions.get(item.id)
            if position is None:
                self.item_ids.append(item.id)
                position = self._known_positions[item.id] = len(self.item_ids)
            results.append({"position": position, **_search_fields(item)})
        summary = (
            f"Searched your wardrobe ({len(rows)} shown)"
            if self.lang == "en"
            else f"搜索了衣橱，显示 {len(rows)} 件"
        )
        return ToolResult({"items": results}, summary)

    def _weather_key(self, args: _WeatherArgs, day: date) -> tuple[date, bool, str]:
        return day, args.when == "now", (args.place or self.location or "").strip()

    def _checked_weather(self, args: _WeatherArgs, day: date) -> WeatherInfo | None:
        reading = self._weather.get(self._weather_key(args, day))
        return reading[0] if reading is not None else None

    def _matches_weather(self, args: _WeatherArgs, day: date) -> bool:
        # Keep recommendations aligned with the retrieved weather when available.
        return self._weather.get(self._weather_key(args, day)) is not None or not any(self._weather.values())

    async def _load_weather(self, args: _WeatherArgs, day: date) -> tuple[WeatherInfo, bool] | None:
        key = self._weather_key(args, day)
        _, current, place = key
        if not place or self.weather is None:
            return None
        if key not in self._weather:
            info = None if current else await self.weather.get_forecast(place, day)
            forecast = info is not None
            if info is None and day == self.today:
                info = await self.weather.get_today(place)
            self._weather[key] = (info, forecast) if info is not None else None
        return self._weather[key]

    async def get_weather(self, raw: dict[str, Any]) -> ToolResult:
        try:
            args = _WeatherArgs.model_validate(raw)
        except ValidationError:
            return ToolResult({"error": "invalid weather arguments"}, "")
        day = _args_date(args.when, self.today)
        if day is None or day < self.today:
            return ToolResult({"error": "date must be today or a future date"}, "")
        if not (args.place or self.location):
            return ToolResult({"error": "no saved location; ask which city"}, "")
        result = await self._load_weather(args, day)
        if result is None:
            return ToolResult({"error": "weather unavailable for this date"}, "")
        info, forecast = result
        condition = sanitize_llm_text(info.condition, max_len=40)
        label = f"{day.isoformat()} {'forecast' if forecast else 'now'}"
        if self.lang != "en":
            label = f"{day.month}月{day.day}日{'预报' if forecast else '实况'}"
        return ToolResult(
            {
                "date": day.isoformat(),
                "kind": "daily_forecast" if forecast else "current",
                "temperature_c": info.temperature_c,
                "condition": condition,
                "rain_probability": info.rain_probability,
                **({} if forecast else {"feels_like_c": info.feels_like_c}),
            },
            f"{label} · {condition + ' ' if condition else ''}{round(info.temperature_c)}°C",
        )

    async def _fetch_wearable(
        self, positions: list[int], *, session: AsyncSession | None = None,
    ) -> list[WardrobeItem] | None:
        ids = [self.item_ids[pos - 1] for pos in positions]
        rows = await (session or self.session).scalars(
            self._owned_items()
            .where(
                WardrobeItem.id.in_(ids),
                WardrobeItem.status == WardrobeItemStatus.WEARABLE.value,
                WardrobeItem.fit_status != FitStatus.NO_LONGER_FITS.value,
            )
            .execution_options(populate_existing=True)
        )
        by_id = {item.id: item for item in rows}
        return [by_id[item_id] for item_id in ids] if len(by_id) == len(ids) else None

    async def validate_outfit(self, raw: dict[str, Any]) -> ToolResult:
        if self.design_constraints is not None and not self.brief_updated:
            return ToolResult({"valid": False, "error": "update_design_brief first"}, "")
        invalid = ToolResult({"valid": False, "error": "invalid outfit arguments"}, "")
        try:
            contextual_defaults: dict[str, object] = {}
            if self.requested_date is not None:
                contextual_defaults["when"] = self.requested_date.isoformat()
            if self.requested_occasion is not None:
                contextual_defaults["occasion"] = self.requested_occasion
            args = _OutfitArgs.model_validate({**contextual_defaults, **raw})
        except ValidationError:
            return invalid
        positions = _positions(args.positions, len(self.item_ids), limit=8)
        day = _args_date(args.when, self.today)
        if positions is None or day is None or day < self.today:
            return invalid
        if self.requested_date is not None and day != self.requested_date:
            return ToolResult({"valid": False, "error": "use the requested date"}, "")
        if self.requested_occasion is not None and args.occasion != self.requested_occasion:
            return ToolResult({"valid": False, "error": "use the requested occasion"}, "")
        if not self._matches_weather(args, day):
            return ToolResult({"valid": False, "error": "use the date and place you looked up"}, "")
        async with self._tool_read_session() as tool_session:
            items = await self._fetch_wearable(positions, session=tool_session)
        if items is None:
            return ToolResult({"valid": False, "error": "one or more pieces cannot be worn"}, "")
        if self.design_constraints is not None:
            required = self.preserved_ids | set(self.design_constraints.locked_item_ids)
            if not required.issubset(item.id for item in items):
                return ToolResult({"valid": False, "error": "retain unchanged and locked pieces"}, "")
        weather = self._checked_weather(args, day)
        issues = _outfit_issues(items, day, args.occasion, weather)
        key = frozenset(positions)
        if issues:
            self._checked.pop(key, None)
            return ToolResult({"valid": False, **issues}, "")
        self._checked[key] = _CheckedOutfit(args, day)
        return ToolResult(
            {"valid": True, "positions": positions, "date": day.isoformat(), "weather_checked": weather is not None},
            "Checked outfit" if self.lang == "en" else "检查了搭配",
        )

    async def resolve_reply(
        self, reply_kind: object, raw_positions: object, *, tools_completed: bool, focus_rejected: bool
    ) -> list[WardrobeItem] | None:
        """Return current approved rows, or None to suppress the entire reply."""
        positions = _positions(raw_positions, len(self.item_ids))
        if positions is None:
            return None
        if reply_kind == "general":
            return [] if not positions else None
        if reply_kind not in ("item_advice", "outfit") or not positions:
            return None
        items = await self._fetch_wearable(positions)
        if items is None:
            return None
        if reply_kind == "item_advice":
            return items if not has_first_outfit_core(item.category for item in items) else None
        if self.focused_item_id is not None and not focus_rejected:
            if self.focused_item_id not in {item.id for item in items}:
                return None
        approval = self._checked.get(frozenset(positions)) if tools_completed else None
        if approval is None or not self._matches_weather(approval.args, approval.target):
            return None
        weather = self._checked_weather(approval.args, approval.target)
        return None if _outfit_issues(items, approval.target, approval.args.occasion, weather) else items
