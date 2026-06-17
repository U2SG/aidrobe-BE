import json
import re

import httpx
import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.application.ports.weather import WeatherInfo
from app.config.settings import Settings
from app.infrastructure.cache.client import get_redis_client

logger = structlog.get_logger(__name__)

# QWeather's /v7/weather/now only accepts a LocationID (digits) or "lng,lat"
# coordinates. A free-text address must first be resolved via GeoAPI.
_LOCATION_ID = re.compile(r"^\d+$")
_COORDS = re.compile(r"^-?\d+(\.\d+)?,-?\d+(\.\d+)?$")
# LocationIDs are stable; cache the geo lookup far longer than the weather itself.
_GEO_CACHE_TTL_SECONDS = 30 * 24 * 3600


class QWeatherProvider:
    def __init__(self, settings: Settings, redis_client: Redis | None = None) -> None:
        if not settings.QWEATHER_API_KEY:
            raise ValueError("QWEATHER_API_KEY is required when WEATHER_PROVIDER=qweather")
        self.settings = settings
        self.redis_client = redis_client or get_redis_client()

    async def get_weather(self, city: str) -> WeatherInfo:
        return await self.get_today(city)

    async def get_today(self, location: str) -> WeatherInfo:
        cache_key = f"weather:qweather:{location}"
        cached = await self._get_cached(cache_key)
        if cached is not None:
            return cached

        location_id = await self._resolve_location_id(location)
        weather = await self._fetch_now(location_id)
        await self._set_cached(cache_key, weather)
        return weather

    async def _resolve_location_id(self, location: str) -> str:
        location = location.strip()
        if _LOCATION_ID.match(location) or _COORDS.match(location):
            return location

        geo_cache_key = f"weather:qweather:geo:{location}"
        cached_id = await self._safe_redis_get(geo_cache_key)
        if cached_id:
            return cached_id

        async with httpx.AsyncClient(timeout=self.settings.WEATHER_TIMEOUT_SECONDS) as client:
            response = await client.get(
                f"{self.settings.QWEATHER_BASE_URL.rstrip('/')}/geo/v2/city/lookup",
                params={"location": location, "key": self.settings.QWEATHER_API_KEY},
            )
            response.raise_for_status()
        payload = response.json()
        matches = payload.get("location") or []
        if not matches:
            raise ValueError(f"QWeather GeoAPI found no location for '{location}' (code={payload.get('code')})")
        location_id = str(matches[0]["id"])
        await self._safe_redis_set(geo_cache_key, location_id, _GEO_CACHE_TTL_SECONDS)
        return location_id

    async def _fetch_now(self, location_id: str) -> WeatherInfo:
        async with httpx.AsyncClient(timeout=self.settings.WEATHER_TIMEOUT_SECONDS) as client:
            response = await client.get(
                f"{self.settings.QWEATHER_BASE_URL.rstrip('/')}/v7/weather/now",
                params={"location": location_id, "key": self.settings.QWEATHER_API_KEY},
            )
            response.raise_for_status()
        now = response.json()["now"]
        return WeatherInfo(
            temperature_c=float(now["temp"]),
            feels_like_c=float(now["feelsLike"]),
            rain_probability=0.0,
            condition=str(now.get("text") or ""),
        )

    async def _get_cached(self, cache_key: str) -> WeatherInfo | None:
        cached = await self._safe_redis_get(cache_key)
        if cached is None:
            return None
        try:
            data = json.loads(cached)
            return WeatherInfo(
                temperature_c=float(data["temperature_c"]),
                feels_like_c=float(data["feels_like_c"]),
                rain_probability=float(data["rain_probability"]),
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
        await self._safe_redis_set(cache_key, payload, self.settings.WEATHER_CACHE_TTL_SECONDS)

    async def _safe_redis_get(self, cache_key: str) -> str | None:
        try:
            cached = await self.redis_client.get(cache_key)
        except RedisError:
            logger.warning("weather_cache_get_failed", cache_key=cache_key)
            return None
        if cached is None:
            return None
        return cached.decode() if isinstance(cached, bytes) else str(cached)

    async def _safe_redis_set(self, cache_key: str, value: str, ttl: int) -> None:
        try:
            await self.redis_client.set(cache_key, value, ex=ttl)
        except RedisError:
            logger.warning("weather_cache_set_failed", cache_key=cache_key)
