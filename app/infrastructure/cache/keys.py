import uuid
from datetime import date, time


def build_daily_recommendation_cache_key(
    user_id: uuid.UUID,
    recommendation_date: date,
    departure_time: time | None = None,
    scene: str | None = None,
) -> str:
    departure_part = departure_time.isoformat() if departure_time is not None else "none"
    scene_part = scene or "none"
    return f"recommendations:today:{user_id}:{recommendation_date.isoformat()}:{departure_part}:{scene_part}"


def build_daily_recommendation_cache_pattern(user_id: uuid.UUID, recommendation_date: date) -> str:
    return f"recommendations:today:{user_id}:{recommendation_date.isoformat()}:*"
