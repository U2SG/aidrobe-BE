import uuid
from datetime import date

from app.infrastructure.cache.keys import build_daily_recommendation_cache_key


def test_daily_recommendation_cache_key_is_partitioned_by_timezone() -> None:
    user_id = uuid.uuid4()
    recommendation_date = date(2026, 7, 15)

    shanghai_key = build_daily_recommendation_cache_key(
        user_id,
        recommendation_date,
        timezone_name="Asia/Shanghai",
    )
    los_angeles_key = build_daily_recommendation_cache_key(
        user_id,
        recommendation_date,
        timezone_name="America/Los_Angeles",
    )

    assert shanghai_key != los_angeles_key
    assert "Asia_Shanghai" in shanghai_key
    assert "America_Los_Angeles" in los_angeles_key
