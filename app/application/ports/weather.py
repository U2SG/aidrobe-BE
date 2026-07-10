from dataclasses import dataclass
from datetime import date
from typing import Protocol


@dataclass(frozen=True)
class WeatherInfo:
    temperature_c: float
    feels_like_c: float
    rain_probability: float
    condition: str
    # Optional enrichments (QWeather). Default None so other providers / older
    # cached payloads / legacy snapshots stay valid.
    humidity: int | None = None
    wind_dir: str | None = None
    wind_scale: str | None = None
    wind_speed: float | None = None
    icon: str | None = None
    location_name: str | None = None

    @property
    def precipitation_probability(self) -> float:
        return self.rain_probability


WeatherSnapshot = WeatherInfo


class WeatherPort(Protocol):
    async def get_weather(self, city: str) -> WeatherInfo: ...

    async def get_today(self, location: str) -> WeatherInfo: ...

    async def get_forecast(self, location: str, target_date: date) -> WeatherInfo | None:
        """BE#77 — daily forecast for a future date (tomorrow mode). Returns None
        when the provider has no forecast for that date — callers must treat that
        as an honest "no weather" degrade, never substitute today's conditions."""
        ...
