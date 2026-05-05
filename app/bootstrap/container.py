from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports.weather import WeatherPort
from app.config.settings import get_settings
from app.infrastructure.cache.client import get_redis_client
from app.infrastructure.importers.router import RoutedLinkParser
from app.infrastructure.persistence.repositories.user_repo import SqlAlchemyUserRepository
from app.infrastructure.push.apns_provider import ApnsProvider
from app.infrastructure.storage.local_storage import LocalObjectStorage
from app.infrastructure.storage.s3_storage import S3ObjectStorage
from app.infrastructure.vision.fake_vision import FakeVision
from app.infrastructure.vision.qwen_vision import QwenVision
from app.infrastructure.vision.rate_limit import InMemoryVisionRateLimiter
from app.infrastructure.weather.openweather import OpenWeatherProvider
from app.infrastructure.weather.qweather import QWeatherProvider

_vision_rate_limiter: InMemoryVisionRateLimiter | None = None


def user_repository(session: AsyncSession) -> SqlAlchemyUserRepository:
    return SqlAlchemyUserRepository(session)


def object_storage() -> LocalObjectStorage | S3ObjectStorage:
    settings = get_settings()
    if settings.STORAGE_PROVIDER == "local":
        return LocalObjectStorage()
    if settings.STORAGE_PROVIDER == "s3":
        return S3ObjectStorage(settings)
    raise ValueError(f"Unsupported storage provider: {settings.STORAGE_PROVIDER}")


def vision_service() -> FakeVision | QwenVision:
    settings = get_settings()
    if settings.VISION_PROVIDER == "fake":
        return FakeVision()
    if settings.VISION_PROVIDER == "qwen":
        return QwenVision(settings)
    raise ValueError(f"Unsupported vision provider: {settings.VISION_PROVIDER}")


def vision_rate_limiter() -> InMemoryVisionRateLimiter:
    global _vision_rate_limiter
    settings = get_settings()
    if _vision_rate_limiter is None:
        _vision_rate_limiter = InMemoryVisionRateLimiter(settings.VISION_MAX_CALLS_PER_USER_HOUR)
    return _vision_rate_limiter


def link_parser() -> RoutedLinkParser:
    return RoutedLinkParser(get_settings())


def weather_service() -> WeatherPort | None:
    settings = get_settings()
    if settings.WEATHER_PROVIDER == "none":
        return None
    if settings.WEATHER_PROVIDER == "qweather":
        return QWeatherProvider(settings)
    if settings.WEATHER_PROVIDER == "openweather":
        return OpenWeatherProvider(settings, get_redis_client())
    raise ValueError(f"Unsupported weather provider: {settings.WEATHER_PROVIDER}")


def push_provider() -> ApnsProvider:
    return ApnsProvider(get_settings())
