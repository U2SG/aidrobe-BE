"""Outfit validation must use the weather evidence requested during the turn."""

import uuid
from datetime import date, timedelta

import pytest
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.application.ask.butler import AskButler
from app.application.ports.weather import WeatherInfo
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem

TODAY = date(2026, 1, 7)


def reading(place: str, temperature: float) -> WeatherInfo:
    return WeatherInfo(temperature, temperature, 0.3, "Cloudy", location_name=place)


class Weather:
    def __init__(self) -> None:
        self.current: dict[str, WeatherInfo] = {}
        self.forecasts: dict[tuple[str, date], WeatherInfo] = {}
        self.current_calls: list[str] = []
        self.raise_for: set[str] = set()

    async def get_weather(self, city: str) -> WeatherInfo:
        return await self.get_today(city)

    async def get_today(self, location: str) -> WeatherInfo:
        self.current_calls.append(location)
        return self.current[location]

    async def get_forecast(self, location: str, target_date: date) -> WeatherInfo | None:
        if location in self.raise_for:
            raise RuntimeError("Weather unavailable")
        return self.forecasts.get((location, target_date))


async def build_butler(
    session: AsyncSession, weather: Weather | None, *, requested_date: date | None = TODAY,
) -> AskButler:
    user_id = uuid.uuid4()
    session.add(User(id=user_id))
    await session.flush()
    items = [
        WardrobeItem(
            user_id=user_id, title=category, category=category, color_main="blue",
            status="wearable", season_tags={"tags": ["all_season"]},
        )
        for category in ("top", "bottom", "shoes", "outerwear")
    ]
    session.add_all(items)
    await session.commit()
    # Tool reads intentionally use another connection, so expose the fixture rows to it.
    assert isinstance(session.bind, AsyncConnection)
    await session.bind.commit()
    return AskButler(
        session, user_id, items, today=TODAY, location="Home", weather=weather,
        lang="en", focused_item_id=None, excluded_ids=set(),
        context={"date": requested_date.isoformat()} if requested_date else None,
    )


@pytest.mark.parametrize("when", [None, "today", TODAY.isoformat()])
async def test_date_validation_reuses_current_weather_city_and_checks_temperature(
    db_session: AsyncSession, when: str | None,
) -> None:
    weather = Weather()
    weather.current["City A"] = reading("City A", 4)
    butler = await build_butler(db_session, weather)
    observed = await butler.get_weather({"place": "City A", "when": "now"})
    assert observed.content["place"] == observed.content["location_name"] == "City A"
    assert observed.content["kind"] == "current" and "City A" in observed.summary
    args = {"when": when} if when else {}

    cold = await butler.validate_outfit({**args, "positions": [1, 2, 3]})
    assert cold.content == {"valid": False, "missing_categories": ["outerwear"]}
    complete = await butler.validate_outfit({**args, "positions": [1, 2, 3, 4]})
    assert complete.content["valid"] is True and complete.content["weather_checked"] is True


async def test_latest_current_weather_takes_precedence_over_earlier_warm_forecast(
    db_session: AsyncSession,
) -> None:
    weather = Weather()
    weather.forecasts["City A", TODAY] = reading("City A", 24)
    weather.current["City A"] = reading("City A", 4)
    butler = await build_butler(db_session, weather)
    await butler.get_weather({"place": "City A", "when": "today"})
    await butler.get_weather({"place": "City A", "when": "now"})

    result = await butler.validate_outfit({"positions": [1, 2, 3], "when": "today"})
    assert result.content == {"valid": False, "missing_categories": ["outerwear"]}


@pytest.mark.parametrize("has_forecast", [True, False])
async def test_now_requires_actual_current_weather_including_today_fallback(
    db_session: AsyncSession, has_forecast: bool,
) -> None:
    weather = Weather()
    weather.current["City A"] = reading("City A", 4)
    if has_forecast:
        weather.forecasts["City A", TODAY] = reading("City A", 24)
    butler = await build_butler(db_session, weather)
    observed = await butler.get_weather({"place": "City A", "when": "today"})
    assert observed.content["kind"] == ("daily_forecast" if has_forecast else "current")

    result = await butler.validate_outfit({"positions": [1, 2, 3, 4], "when": "now"})
    assert result.content["valid"] is not has_forecast
    if not has_forecast:
        assert result.content["weather_checked"] is True


async def test_cached_repeat_becomes_latest_city_but_explicit_city_stays_independent(
    db_session: AsyncSession,
) -> None:
    weather = Weather()
    weather.current = {"City A": reading("City A", 4), "City B": reading("City B", 24)}
    butler = await build_butler(db_session, weather)
    for city in ("City A", "City B", "City A"):
        await butler.get_weather({"place": city, "when": "now"})
    assert weather.current_calls == ["City A", "City B"]

    latest = await butler.validate_outfit({"positions": [1, 2, 3]})
    assert latest.content == {"valid": False, "missing_categories": ["outerwear"]}
    explicit = await butler.validate_outfit({"positions": [1, 2, 3], "place": "City B"})
    assert explicit.content["valid"] is True


async def test_weather_cannot_cross_explicit_city_or_date(db_session: AsyncSession) -> None:
    tomorrow = TODAY + timedelta(days=1)
    weather = Weather()
    weather.forecasts["City A", tomorrow] = reading("City A", 4)
    butler = await build_butler(db_session, weather, requested_date=None)
    await butler.get_weather({"place": "City A", "when": "tomorrow"})

    for args in ({"place": "City A", "when": "today"}, {"place": "City B", "when": "tomorrow"}):
        result = await butler.validate_outfit({**args, "positions": [1, 2, 3, 4]})
        assert result.content["valid"] is False
    correct = await butler.validate_outfit({"place": "City A", "when": "tomorrow", "positions": [1, 2, 3, 4]})
    assert correct.content["valid"] is True and correct.content["date"] == tomorrow.isoformat()


async def test_approved_outfit_stays_bound_to_its_city_until_revalidated(db_session: AsyncSession) -> None:
    weather = Weather()
    weather.current = {"City A": reading("City A", 24), "City B": reading("City B", 4)}
    butler = await build_butler(db_session, weather)
    await butler.get_weather({"place": "City A", "when": "now"})
    assert (await butler.validate_outfit({"positions": [1, 2, 3]})).content["valid"] is True
    await butler.get_weather({"place": "City B", "when": "now"})

    approved = await butler.resolve_reply("outfit", [1, 2, 3], tools_completed=True, focus_rejected=False)
    assert approved is not None
    assert [item.category for item in approved] == ["top", "bottom", "shoes"]
    assert (await butler.validate_outfit({"positions": [1, 2, 3]})).content["valid"] is False
    assert await butler.resolve_reply("outfit", [1, 2, 3], tools_completed=True, focus_rejected=False) is None


@pytest.mark.parametrize("raises", [False, True])
async def test_failed_latest_city_does_not_borrow_other_city_or_keep_old_approval(
    db_session: AsyncSession, raises: bool,
) -> None:
    tomorrow = TODAY + timedelta(days=1)
    weather = Weather()
    weather.forecasts["City A", tomorrow] = reading("City A", 24)
    if raises:
        weather.raise_for.add("City B")
    butler = await build_butler(db_session, weather, requested_date=tomorrow)
    await butler.get_weather({"place": "City A", "when": "tomorrow"})
    assert (await butler.validate_outfit({"positions": [1, 2, 3]})).content["valid"] is True
    failure = await butler.registry.execute("get_weather", {"place": "City B", "when": "tomorrow"})
    assert "error" in failure.content

    result = await butler.validate_outfit({"positions": [1, 2, 3]})
    assert result.content["valid"] is False
    assert await butler.resolve_reply("outfit", [1, 2, 3], tools_completed=True, focus_rejected=False) is None


@pytest.mark.parametrize("provider_available", [False, True])
async def test_no_available_weather_preserves_honest_degradation(
    db_session: AsyncSession, provider_available: bool,
) -> None:
    weather = Weather() if provider_available else None
    butler = await build_butler(db_session, weather, requested_date=TODAY + timedelta(days=1))
    failure = await butler.get_weather({"place": "City A", "when": "tomorrow"})
    assert "error" in failure.content
    assert failure.content["place"] == "City A"

    result = await butler.validate_outfit({"positions": [1, 2, 3]})
    assert result.content["valid"] is True and result.content["weather_checked"] is False
    if weather is not None:
        assert weather.current_calls == []
