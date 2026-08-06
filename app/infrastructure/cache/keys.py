import uuid
from datetime import date, time

# Version segment for the cached /today payload shape. Bump whenever
# TodayRecommendationResponse gains fields that the cache-hit path must
# populate — otherwise entries written by the previous deploy validate
# successfully with null defaults and serve incomplete payloads until the
# day-end TTL. "p2" = recommendation identity metadata (BE#131); "p3" = trace_id
# actually populated now that decision traces are persisted.
_PAYLOAD_VERSION = "p3"


def build_daily_recommendation_cache_key(
    user_id: uuid.UUID,
    recommendation_date: date,
    departure_time: time | None = None,
    scene: str | None = None,
    timezone_name: str | None = None,
    reasoning: bool = False,
    vision_style: bool = False,
) -> str:
    departure_part = departure_time.isoformat() if departure_time is not None else "none"
    scene_part = scene or "none"
    timezone_part = (timezone_name or "default").replace("/", "_")
    # `reasoning` / `vision_style` segment the key by ENABLE_TODAY_REASONING /
    # ENABLE_VISION_STYLE_LEARNING so flipping a flag serves from a different key
    # rather than leaking a stale baked-in field (e.g. a vision-derived
    # style_context template) until the TTL expires. The invalidation pattern
    # below is a prefix wildcard that still clears all variants on
    # settings/location change.
    reasoning_part = "r1" if reasoning else "r0"
    vision_part = "v1" if vision_style else "v0"
    return (
        f"recommendations:today:{user_id}:{recommendation_date.isoformat()}"
        f":{departure_part}:{scene_part}:{timezone_part}:{reasoning_part}:{vision_part}"
        f":{_PAYLOAD_VERSION}"
    )


def build_daily_recommendation_cache_pattern(user_id: uuid.UUID, recommendation_date: date) -> str:
    return f"recommendations:today:{user_id}:{recommendation_date.isoformat()}:*"


def build_user_recommendation_cache_pattern(user_id: uuid.UUID) -> str:
    return f"recommendations:today:{user_id}:*"
