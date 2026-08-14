"""V3-011 — the avatar retention policy, and the flag that gates the feature.

The V3-011 human gate reads: "Final retention duration requires privacy
approval; code must use configurable values." These tests are the mechanical
half of that — they prove no duration is hard-coded and that changing the
configured value changes the answer. The *durations themselves* are still
unapproved placeholders; nothing here asserts that 30 days is correct, only
that 30 days is not baked in.

Pure functions, no DB — runnable with `--noconftest`.
"""

from datetime import UTC, datetime, timedelta

from fastapi import FastAPI

from app.application.avatar.retention import (
    is_due,
    retired_version_purge_due_at,
    source_asset_purge_due_at,
)
from app.config.settings import Settings

CREATED = datetime(2026, 8, 14, 12, 0, tzinfo=UTC)


def _settings(**overrides: object) -> Settings:
    return Settings(**overrides)  # type: ignore[arg-type]


def test_source_photo_deadline_comes_from_configuration_not_from_a_constant() -> None:
    default = _settings()
    assert source_asset_purge_due_at(CREATED, default) == CREATED + timedelta(
        days=default.AVATAR_SOURCE_RETENTION_DAYS
    )

    # The gate's actual requirement: an operator (or the privacy decision)
    # changes the value and the policy follows, with no code change.
    shortened = _settings(AVATAR_SOURCE_RETENTION_DAYS=7)
    assert source_asset_purge_due_at(CREATED, shortened) == CREATED + timedelta(days=7)
    assert source_asset_purge_due_at(CREATED, shortened) != source_asset_purge_due_at(CREATED, default)


def test_retired_version_deadline_comes_from_its_own_setting() -> None:
    """Source photos and derived versions are separately configurable.

    They are different kinds of data with different privacy weight; a single
    knob would force the sensitive input and the derived artifact onto one
    schedule.
    """
    settings = _settings(AVATAR_SOURCE_RETENTION_DAYS=30, AVATAR_RETIRED_VERSION_RETENTION_DAYS=3)
    retired = CREATED + timedelta(days=10)
    assert retired_version_purge_due_at(retired, settings) == retired + timedelta(days=3)


def test_a_live_version_is_never_on_a_retention_clock() -> None:
    assert retired_version_purge_due_at(None, _settings()) is None


def test_zero_or_negative_days_means_keep_not_purge_immediately() -> None:
    """A misconfigured `0` must not read as "delete everything now"."""
    for days in (0, -1):
        settings = _settings(AVATAR_SOURCE_RETENTION_DAYS=days, AVATAR_RETIRED_VERSION_RETENTION_DAYS=days)
        assert source_asset_purge_due_at(CREATED, settings) is None
        assert retired_version_purge_due_at(CREATED, settings) is None
        assert is_due(source_asset_purge_due_at(CREATED, settings), CREATED + timedelta(days=3650)) is False


def test_is_due_is_inclusive_at_the_deadline() -> None:
    settings = _settings(AVATAR_SOURCE_RETENTION_DAYS=30)
    deadline = source_asset_purge_due_at(CREATED, settings)
    assert deadline is not None
    assert is_due(deadline, deadline - timedelta(seconds=1)) is False
    assert is_due(deadline, deadline) is True


def test_naive_timestamps_are_read_as_utc_rather_than_crashing() -> None:
    """Postgres hands back aware datetimes, but a caller constructing one by
    hand should not get a TypeError from comparing naive to aware."""
    settings = _settings(AVATAR_SOURCE_RETENTION_DAYS=1)
    naive = datetime(2026, 8, 14, 12, 0)
    assert source_asset_purge_due_at(naive, settings) == CREATED + timedelta(days=1)
    assert is_due(CREATED, datetime(2026, 8, 14, 12, 0)) is True


def test_the_v3_avatar_flag_defaults_off() -> None:
    """Contract §2: V3 flags default off, and rows persist regardless."""
    assert _settings().ENABLE_V3_AVATAR is False


def test_the_avatar_routes_are_registered_and_gated_elsewhere() -> None:
    """V3-013 has landed the routes this test used to assert were absent.

    The V3-011 version read `assert not [... "avatar" ...]` and said in its own
    docstring that it existed "to catch an endpoint being smuggled in ahead of
    V3-013, which owns the consent / prepare / replace / delete APIs and their
    503-when-off gate". V3-013 is that feature, so the placeholder is being
    *fulfilled* here rather than deleted: the successor assertion is that the
    frozen §4.2 set is registered, no more and no less, which still catches a
    seventh avatar surface appearing without review.

    The assertion the old one was standing in for — that every surface answers
    503 while `ENABLE_V3_AVATAR` is off — is real now that endpoints exist, and
    lives at
    `tests/api/test_v3_avatar_profile_api.py::test_every_surface_is_503_when_the_flag_is_off`.
    It could not be written from here: with no route there was nothing to gate,
    which is exactly the caveat V3-011 recorded (and V3-003 before it).
    """
    from app.main import create_app

    app: FastAPI = create_app()
    paths = {getattr(route, "path", "") for route in app.routes}

    assert {path for path in paths if "avatar" in path} == {
        "/v1/avatar-profile",
        "/v1/avatar-candidates/{source_asset_id}/consent",
        "/v1/avatar-profile/uploads",
        "/v1/avatar-profile/prepare",
        "/v1/avatar-profile/replace",
    }
