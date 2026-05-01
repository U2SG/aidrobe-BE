import uuid
from datetime import UTC, datetime
from functools import lru_cache
from typing import cast

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config.settings import get_settings
from app.infrastructure.cache.keys import build_daily_recommendation_cache_pattern

logger = structlog.get_logger(__name__)


@lru_cache
def _redis_client() -> Redis:  # type: ignore[type-arg]
    settings = get_settings()
    return cast(Redis, Redis.from_url(settings.REDIS_URL, decode_responses=True))  # type: ignore[type-arg]


def get_redis_client() -> Redis:  # type: ignore[type-arg]
    return _redis_client()


async def close_redis_client() -> None:
    client = _redis_client()
    await client.aclose()
    _redis_client.cache_clear()


async def invalidate_daily_recommendation_cache(redis_client: Redis, user_id: uuid.UUID) -> None:  # type: ignore[type-arg]
    today = datetime.now(UTC).date()
    pattern = build_daily_recommendation_cache_pattern(user_id, today)
    try:
        keys = [key async for key in redis_client.scan_iter(match=pattern)]
        if keys:
            await redis_client.delete(*keys)
    except RedisError:
        logger.warning("daily_recommendation_cache_invalidation_failed", user_id=str(user_id))
