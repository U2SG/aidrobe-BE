"""Read-only wardrobe tools with turn-local item positions and outfit approvals."""

from __future__ import annotations

import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, timedelta
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from sqlalchemy import Select, func, nulls_last, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ask.tools import AskTool, ToolRegistry
from app.application.ports.llm import ToolResult, ToolSpec
from app.application.ports.weather import WeatherInfo, WeatherPort
from app.application.recommendations.engine import BUSINESS_FORMALITY, CASUAL_FORMALITY, COLD_FEELS_LIKE_C
from app.application.recommendations.feasibility import has_first_outfit_core, missing_first_outfit_slots
from app.common.enums import Category, FitScene, FitStatus, WardrobeItemStatus
from app.common.season import SeasonName, current_season, in_season, season_tags_of
from app.infrastructure.llm.sanitizer import sanitize_for_llm, sanitize_llm_text
from app.infrastructure.persistence.models.wardrobe import WardrobeItem

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
    limit: int = Field(default=12, ge=1, le=20)


class _OutfitArgs(_WeatherArgs):
    positions: list[Annotated[int, Field(strict=True, ge=1)]] = Field(min_length=2, max_length=8)
    occasion: FitScene | None = None


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
    }


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
    ) -> None:
        self.session = session
        self.user_id = user_id
        self.today = today
        self.location = location
        self.weather = weather
        self.lang = lang
        self.focused_item_id = focused_item_id
        self.excluded_ids: set[uuid.UUID] = set()
        for item_id in excluded_ids:
            try:
                self.excluded_ids.add(uuid.UUID(item_id))
            except ValueError:
                continue
        raw_date = (context or {}).get("date")
        self.requested_date = _args_date(raw_date, today) if isinstance(raw_date, str) else None
        if self.requested_date is not None and self.requested_date < today:
            self.requested_date = None
        raw_occasion = (context or {}).get("scene")
        self.requested_occasion = next((scene for scene in FitScene if scene.value == raw_occasion), None)
        self.item_ids = [item.id for item in items]
        self._known_positions = {item_id: pos for pos, item_id in enumerate(self.item_ids, start=1)}
        self._weather: dict[tuple[date, bool, str], tuple[WeatherInfo, bool] | None] = {}
        self._checked: dict[frozenset[int], _CheckedOutfit] = {}
        self.registry = ToolRegistry([
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
        ])

    @property
    def prompt_context(self) -> str:
        details = f"\nLocal date: {self.today.isoformat()}."
        if self.requested_date is not None:
            details += f" Requested outfit date: {self.requested_date.isoformat()}."
        if self.requested_occasion is not None:
            details += f" Requested occasion: {self.requested_occasion.value}."
        return details

    def _owned_items(self) -> Select[tuple[WardrobeItem]]:
        return select(WardrobeItem).where(
            WardrobeItem.user_id == self.user_id,
            WardrobeItem.deleted_at.is_(None),
            WardrobeItem.id.not_in(self.excluded_ids),
        )

    async def search_wardrobe(self, raw: dict[str, Any]) -> ToolResult:
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
        rows = list(await self.session.scalars(
            query.order_by(
                WardrobeItem.wear_count.desc(), nulls_last(WardrobeItem.last_worn_at.desc()), WardrobeItem.id
            ).limit(args.limit)
        ))
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

    async def _fetch_wearable(self, positions: list[int]) -> list[WardrobeItem] | None:
        ids = [self.item_ids[pos - 1] for pos in positions]
        rows = await self.session.scalars(
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
        items = await self._fetch_wearable(positions)
        if items is None:
            return ToolResult({"valid": False, "error": "one or more pieces cannot be worn"}, "")
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
