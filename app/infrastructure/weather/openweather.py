import json
from datetime import date

import httpx
import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.application.ports.weather import WeatherInfo
from app.config.settings import Settings
from app.infrastructure.cache.client import get_redis_client

logger = structlog.get_logger(__name__)


class OpenWeatherProvider:
    def __init__(self, settings: Settings, redis_client: Redis | None = None) -> None:
        if not settings.OPENWEATHER_API_KEY:
            raise ValueError("OPENWEATHER_API_KEY is required when WEATHER_PROVIDER=openweather")
        self.settings = settings
        self.redis_client = redis_client or get_redis_client()

    async def get_today(self, location: str) -> WeatherInfo:
        return await self.get_weather(location)

    async def get_forecast(self, location: str, target_date: date) -> WeatherInfo | None:
        # BE#77 — no forecast path wired for OpenWeather yet; honest degrade
        # (None) rather than current conditions dressed up as a forecast.
        return None

    async def get_weather(self, city: str) -> WeatherInfo:
        cache_key = f"weather:{city}"
        cached = await self._get_cached(cache_key)
        if cached is not None:
            return cached

        weather = await self._fetch_weather(city)
        await self._set_cached(cache_key, weather)
        return weather

    async def _get_cached(self, cache_key: str) -> WeatherInfo | None:
        try:
            cached = await self.redis_client.get(cache_key)
        except RedisError:
            logger.warning("weather_cache_get_failed", cache_key=cache_key)
            return None
        if cached is None:
            return None
        payload = cached.decode() if isinstance(cached, bytes) else cached
        try:
            data = json.loads(payload)
            return WeatherInfo(
                temperature_c=float(data["temperature_c"]),
                feels_like_c=float(data["feels_like_c"]),
                rain_probability=_clamp_probability(float(data["rain_probability"])),
                condition=str(data["condition"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            logger.warning("weather_cache_payload_invalid", cache_key=cache_key)
            return None

    async def _set_cached(self, cache_key: str, weather: WeatherInfo) -> None:
        payload = json.dumps(
            {
                "temperature_c": weather.temperature_c,
                "feels_like_c": weather.feels_like_c,
                "rain_probability": weather.rain_probability,
                "condition": weather.condition,
            }
        )
        try:
            await self.redis_client.set(
                cache_key,
                payload,
                ex=self.settings.WEATHER_CACHE_TTL_SECONDS,
            )
        except RedisError:
            logger.warning("weather_cache_set_failed", cache_key=cache_key)

    async def _fetch_weather(self, city: str) -> WeatherInfo:
        async with httpx.AsyncClient(timeout=self.settings.WEATHER_TIMEOUT_SECONDS) as client:
            response = await client.get(
                "https://api.openweathermap.org/data/2.5/forecast",
                params={
                    "q": city,
                    "appid": self.settings.OPENWEATHER_API_KEY,
                    "units": "metric",
                },
            )
            response.raise_for_status()
        data = response.json()
        forecast = (data.get("list") or [{}])[0]
        main = forecast.get("main") or {}
        weather_items = forecast.get("weather") or [{}]
        condition = str(weather_items[0].get("main") or weather_items[0].get("description") or "")
        return WeatherInfo(
            temperature_c=float(main["temp"]),
            feels_like_c=float(main["feels_like"]),
            rain_probability=_clamp_probability(float(forecast.get("pop") or 0.0)),
            condition=condition,
        )


def _clamp_probability(value: float) -> float:
    return min(1.0, max(0.0, value))
