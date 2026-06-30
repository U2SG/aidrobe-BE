import uuid
from datetime import date, time


def build_daily_recommendation_cache_key(
    user_id: uuid.UUID,
    recommendation_date: date,
    departure_time: time | None = None,
    scene: str | None = None,
    reasoning: bool = False,
    vision_style: bool = False,
) -> str:
    departure_part = departure_time.isoformat() if departure_time is not None else "none"
    scene_part = scene or "none"
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
        f":{departure_part}:{scene_part}:{reasoning_part}:{vision_part}"
    )


def build_daily_recommendation_cache_pattern(user_id: uuid.UUID, recommendation_date: date) -> str:
    return f"recommendations:today:{user_id}:{recommendation_date.isoformat()}:*"
