from datetime import UTC, datetime

import pytest

from app.common.local_time import (
    local_datetime,
    seconds_until_local_day_end,
    validate_timezone_name,
)


def test_local_datetime_crosses_utc_date_boundary() -> None:
    now = datetime(2026, 7, 15, 16, 30, tzinfo=UTC)

    local = local_datetime(now, "Asia/Singapore", "UTC")

    assert local.isoformat() == "2026-07-16T00:30:00+08:00"


def test_local_datetime_uses_default_for_unset_user_timezone() -> None:
    now = datetime(2026, 7, 15, 16, 30, tzinfo=UTC)

    local = local_datetime(now, None, "Asia/Shanghai")

    assert local.isoformat() == "2026-07-16T00:30:00+08:00"


def test_seconds_until_local_day_end_honors_dst_transition() -> None:
    # 2026-03-08 is the spring-forward day in New York. At 01:30 EST there
    # are only 21.5 real hours until the next local midnight (EDT), not 22.5.
    now = datetime(2026, 3, 8, 6, 30, tzinfo=UTC)

    seconds = seconds_until_local_day_end(now, "America/New_York", "UTC")

    assert seconds == 77_400


def test_validate_timezone_name_rejects_unknown_iana_zone() -> None:
    with pytest.raises(ValueError, match="IANA timezone"):
        validate_timezone_name("Mars/Olympus_Mons")


def test_validate_timezone_name_rejects_absolute_path() -> None:
    with pytest.raises(ValueError, match="IANA timezone"):
        validate_timezone_name("/etc/localtime")
