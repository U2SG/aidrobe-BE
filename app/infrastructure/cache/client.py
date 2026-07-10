import uuid
from datetime import UTC, datetime, timedelta
from functools import lru_cache
from typing import cast

import structlog
from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config.settings import get_settings
from app.infrastructure.cache.keys import build_daily_recommendation_cache_pattern

logger = structlog.get_logger(__name__)


@lru_cache
def _redis_client() -> Redis:
    settings = get_settings()
    return cast(Redis, Redis.from_url(settings.REDIS_URL, decode_responses=True))


def get_redis_client() -> Redis:
    return _redis_client()


async def close_redis_client() -> None:
    client = _redis_client()
    await client.aclose()
    _redis_client.cache_clear()


async def invalidate_daily_recommendation_cache(redis_client: Redis, user_id: uuid.UUID) -> None:
    # BE#77 made Today date-aware, so a cached TOMORROW payload goes stale on
    # exactly the same mutations as today's — invalidate both days' patterns.
    today = datetime.now(UTC).date()
    try:
        for target in (today, today + timedelta(days=1)):
            pattern = build_daily_recommendation_cache_pattern(user_id, target)
            keys = [key async for key in redis_client.scan_iter(match=pattern)]
            if keys:
                await redis_client.delete(*keys)
    except RedisError:
        logger.warning("daily_recommendation_cache_invalidation_failed", user_id=str(user_id))
