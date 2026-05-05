from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class WeatherInfo:
    temperature_c: float
    feels_like_c: float
    rain_probability: float
    condition: str

    @property
    def precipitation_probability(self) -> float:
        return self.rain_probability


WeatherSnapshot = WeatherInfo


class WeatherPort(Protocol):
    async def get_weather(self, city: str) -> WeatherInfo: ...

    async def get_today(self, location: str) -> WeatherInfo: ...
