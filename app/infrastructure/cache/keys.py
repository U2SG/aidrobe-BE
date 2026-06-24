import uuid
from datetime import date, time


def build_daily_recommendation_cache_key(
    user_id: uuid.UUID,
    recommendation_date: date,
    departure_time: time | None = None,
    scene: str | None = None,
    reasoning: bool = False,
) -> str:
    departure_part = departure_time.isoformat() if departure_time is not None else "none"
    scene_part = scene or "none"
    # `reasoning` segments the key by ENABLE_TODAY_REASONING so flipping the flag
    # serves from a different key rather than leaking a stale baked-in field
    # until the TTL expires. The invalidation pattern below is a prefix wildcard
    # that still clears both variants on settings/location change.
    reasoning_part = "r1" if reasoning else "r0"
    return (
        f"recommendations:today:{user_id}:{recommendation_date.isoformat()}"
        f":{departure_part}:{scene_part}:{reasoning_part}"
    )


def build_daily_recommendation_cache_pattern(user_id: uuid.UUID, recommendation_date: date) -> str:
    return f"recommendations:today:{user_id}:{recommendation_date.isoformat()}:*"
