"""BE#77 — generator weather loading is date-aware (pure, fake port/repo)."""

import uuid
from datetime import UTC, date, datetime, timedelta

from app.application.ports.weather import WeatherInfo
from app.application.recommendations.generator import RecommendationGenerator

_TODAY = datetime.now(UTC).date()


def _weather(condition: str) -> WeatherInfo:
    return WeatherInfo(
        temperature_c=25.0, feels_like_c=25.0, rain_probability=0.0, condition=condition
    )


class _FakeRepo:
    session = None

    async def get_user_location(self, user_id: uuid.UUID) -> str:
        return "beijing"


class _FakeWeather:
    def __init__(self, forecast: WeatherInfo | None) -> None:
        self._forecast = forecast

    async def get_weather(self, city: str) -> WeatherInfo:
        return _weather("current")

    async def get_today(self, location: str) -> WeatherInfo:  # pragma: no cover
        return _weather("current")

    async def get_forecast(self, location: str, target_date: date) -> WeatherInfo | None:
        return self._forecast


def _generator(forecast: WeatherInfo | None) -> RecommendationGenerator:
    return RecommendationGenerator(_FakeRepo(), weather_service=_FakeWeather(forecast))


async def test_today_uses_current_weather() -> None:
    gen = _generator(forecast=_weather("forecast"))
    weather = await gen._load_weather(uuid.uuid4(), _TODAY)
    assert weather is not None
    assert weather.condition == "current"


async def test_tomorrow_uses_forecast() -> None:
    gen = _generator(forecast=_weather("forecast"))
    weather = await gen._load_weather(uuid.uuid4(), _TODAY + timedelta(days=1))
    assert weather is not None
    assert weather.condition == "forecast"


async def test_tomorrow_without_forecast_degrades_to_none() -> None:
    # Honest degrade: no forecast → no weather, never today's conditions.
    gen = _generator(forecast=None)
    assert await gen._load_weather(uuid.uuid4(), _TODAY + timedelta(days=1)) is None
