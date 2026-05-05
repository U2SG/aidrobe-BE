import httpx

from app.application.ports.weather import WeatherInfo
from app.config.settings import Settings


class QWeatherProvider:
    def __init__(self, settings: Settings) -> None:
        if not settings.QWEATHER_API_KEY:
            raise ValueError("QWEATHER_API_KEY is required when WEATHER_PROVIDER=qweather")
        self.settings = settings

    async def get_weather(self, city: str) -> WeatherInfo:
        return await self.get_today(city)

    async def get_today(self, location: str) -> WeatherInfo:
        async with httpx.AsyncClient(timeout=self.settings.WEATHER_TIMEOUT_SECONDS) as client:
            response = await client.get(
                f"{self.settings.QWEATHER_BASE_URL.rstrip('/')}/v7/weather/now",
                params={"location": location, "key": self.settings.QWEATHER_API_KEY},
            )
            response.raise_for_status()
        now = response.json()["now"]
        return WeatherInfo(
            temperature_c=float(now["temp"]),
            feels_like_c=float(now["feelsLike"]),
            rain_probability=0.0,
            condition=str(now.get("text") or ""),
        )
