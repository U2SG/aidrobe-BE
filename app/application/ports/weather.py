from dataclasses import dataclass
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
