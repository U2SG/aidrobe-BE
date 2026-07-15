from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


def validate_timezone_name(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ValueError, ZoneInfoNotFoundError) as exc:
        raise ValueError("timezone must be a valid IANA timezone") from exc
    return value


def resolved_timezone(timezone_name: str | None, default_timezone: str) -> ZoneInfo:
    return ZoneInfo(validate_timezone_name(timezone_name or default_timezone))


def utc_now() -> datetime:
    return datetime.now(UTC)


def local_datetime(
    now: datetime,
    timezone_name: str | None,
    default_timezone: str,
) -> datetime:
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    return now.astimezone(resolved_timezone(timezone_name, default_timezone))


def seconds_until_local_day_end(
    now: datetime,
    timezone_name: str | None,
    default_timezone: str,
) -> int:
    local_now = local_datetime(now, timezone_name, default_timezone)
    tomorrow = local_now.date() + timedelta(days=1)
    next_midnight = datetime.combine(tomorrow, time.min, tzinfo=local_now.tzinfo)
    remaining = next_midnight.astimezone(UTC) - now.astimezone(UTC)
    return max(1, int(remaining.total_seconds()))
