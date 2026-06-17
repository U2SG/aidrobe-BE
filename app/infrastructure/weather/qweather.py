import asyncio
import json
import re
from dataclasses import asdict
from typing import Any

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
# /v7/weather/now has no precipitation probability; pull it from the hourly
# forecast and take the worst case over the near-term window.
_POP_WINDOW_HOURS = 12


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

        location_id, location_name = await self._resolve_location_id(location)
        # now + 24h forecast (+ best-effort reverse name lookup for the
        # coords/LocationID path) run concurrently — pop must not double latency.
        now_task = asyncio.create_task(self._fetch_now(location_id))
        pop_task = asyncio.create_task(self._fetch_pop(location_id))
        name_task = (
            asyncio.create_task(self._lookup_name(location_id)) if location_name is None else None
        )
        now_fields = await now_task
        rain_probability = await pop_task
        if name_task is not None:
            location_name = await name_task

        weather = WeatherInfo(
            temperature_c=now_fields["temperature_c"],
            feels_like_c=now_fields["feels_like_c"],
            rain_probability=rain_probability,
            condition=now_fields["condition"],
            humidity=now_fields["humidity"],
            wind_dir=now_fields["wind_dir"],
            wind_scale=now_fields["wind_scale"],
            wind_speed=now_fields["wind_speed"],
            icon=now_fields["icon"],
            location_name=location_name,
        )
        await self._set_cached(cache_key, weather)
        return weather

    async def _resolve_location_id(self, location: str) -> tuple[str, str | None]:
        location = location.strip()
        if _LOCATION_ID.match(location) or _COORDS.match(location):
            return location, None
        match = await self._geo_top_match(location)
        if match is None:
            raise ValueError(f"QWeather GeoAPI found no location for '{location}'")
        return match

    async def _lookup_name(self, query: str) -> str | None:
        """Best-effort city name for the coords/LocationID path. A failure here
        must never sink the weather fetch, so swallow everything."""
        try:
            match = await self._geo_top_match(query)
        except Exception as exc:
            logger.warning("qweather_geo_name_failed", error=str(exc))
            return None
        return match[1] if match else None

    async def _geo_top_match(self, query: str) -> tuple[str, str | None] | None:
        cache_key = f"weather:qweather:geo:{query}"
        cached = await self._safe_redis_get(cache_key)
        if cached:
            data = json.loads(cached)
            return str(data["id"]), data.get("name")

        async with httpx.AsyncClient(timeout=self.settings.WEATHER_TIMEOUT_SECONDS) as client:
            response = await client.get(
                f"{self.settings.QWEATHER_BASE_URL.rstrip('/')}/geo/v2/city/lookup",
                params={"location": query, "key": self.settings.QWEATHER_API_KEY},
            )
            response.raise_for_status()
        matches = response.json().get("location") or []
        if not matches:
            return None
        result = (str(matches[0]["id"]), _opt_str(matches[0].get("name")))
        await self._safe_redis_set(cache_key, json.dumps({"id": result[0], "name": result[1]}), _GEO_CACHE_TTL_SECONDS)
        return result

    async def _fetch_now(self, location_id: str) -> dict[str, Any]:
        async with httpx.AsyncClient(timeout=self.settings.WEATHER_TIMEOUT_SECONDS) as client:
            response = await client.get(
                f"{self.settings.QWEATHER_BASE_URL.rstrip('/')}/v7/weather/now",
                params={"location": location_id, "key": self.settings.QWEATHER_API_KEY},
            )
            response.raise_for_status()
        now = response.json()["now"]
        # temp/feelsLike are core — let a malformed payload raise. The rest are
        # marked "可能为空" by QWeather, so parse them defensively to None.
        return {
            "temperature_c": float(now["temp"]),
            "feels_like_c": float(now["feelsLike"]),
            "condition": str(now.get("text") or ""),
            "humidity": _opt_int(now.get("humidity")),
            "wind_dir": _opt_str(now.get("windDir")),
            "wind_scale": _opt_str(now.get("windScale")),
            "wind_speed": _opt_float(now.get("windSpeed")),
            "icon": _opt_str(now.get("icon")),
        }

    async def _fetch_pop(self, location_id: str) -> float:
        """Max precipitation probability over the near-term hourly window.
        Best-effort: a forecast failure degrades to 0.0 rather than sinking the
        whole weather fetch (temp/condition are still useful)."""
        try:
            async with httpx.AsyncClient(timeout=self.settings.WEATHER_TIMEOUT_SECONDS) as client:
                response = await client.get(
                    f"{self.settings.QWEATHER_BASE_URL.rstrip('/')}/v7/weather/24h",
                    params={"location": location_id, "key": self.settings.QWEATHER_API_KEY},
                )
                response.raise_for_status()
            hourly = response.json().get("hourly") or []
            values = [
                pop
                for pop in (_opt_float(hour.get("pop")) for hour in hourly[:_POP_WINDOW_HOURS])
                if pop is not None
            ]
            return _clamp_probability(max(values) / 100.0) if values else 0.0
        except Exception as exc:
            logger.warning("qweather_pop_fetch_failed", error=str(exc))
            return 0.0

    async def _get_cached(self, cache_key: str) -> WeatherInfo | None:
        cached = await self._safe_redis_get(cache_key)
        if cached is None:
            return None
        try:
            return WeatherInfo(**json.loads(cached))
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            logger.warning("weather_cache_payload_invalid", cache_key=cache_key)
            return None

    async def _set_cached(self, cache_key: str, weather: WeatherInfo) -> None:
        payload = json.dumps(asdict(weather))
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


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _opt_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _opt_float(value: Any) -> float | None:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


def _clamp_probability(value: float) -> float:
    return min(1.0, max(0.0, value))
