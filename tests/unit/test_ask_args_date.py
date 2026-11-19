"""Tool date arguments: ISO dates, relative words, and ISO dates the model decorated."""

from datetime import date

import pytest

from app.application.ask.butler import _args_date

TODAY = date(2026, 11, 19)


@pytest.mark.parametrize(
    ("when", "expected"),
    [
        ("today", TODAY),
        ("now", TODAY),
        ("tomorrow", date(2026, 11, 20)),
        ("2026-11-20", date(2026, 11, 20)),
        ("周五（2026-11-20）", date(2026, 11, 20)),
        ("Friday 2026-11-20", date(2026, 11, 20)),
        ("2026-11-20 到 2026-11-20", date(2026, 11, 20)),
        ("周五", None),
        ("20261120", None),
        ("2026-11-20 到 2026-11-21", None),
        ("2026-02-30", None),
        ("12026-11-20", None),
    ],
)
def test_args_date(when: str, expected: date | None) -> None:
    assert _args_date(when, TODAY) == expected
