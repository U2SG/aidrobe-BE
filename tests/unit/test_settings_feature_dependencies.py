import pytest
from pydantic import ValidationError

from app.config.settings import Settings


def test_context_overrides_require_today_refresh() -> None:
    with pytest.raises(
        ValidationError,
        match="ENABLE_CONTEXT_OVERRIDES requires ENABLE_TODAY_REFRESH",
    ):
        Settings(
            APP_ENV="dev",
            ENABLE_CONTEXT_OVERRIDES=True,
            ENABLE_TODAY_REFRESH=False,
        )


def test_context_overrides_allow_valid_dependency_combination() -> None:
    settings = Settings(
        APP_ENV="dev",
        ENABLE_CONTEXT_OVERRIDES=True,
        ENABLE_TODAY_REFRESH=True,
    )

    assert settings.ENABLE_CONTEXT_OVERRIDES is True


def test_static_tryon_requires_the_avatar_lifecycle() -> None:
    """V3-017 — contract §2 rule 5, refused at startup rather than at runtime.

    A static try-on renders the user's own avatar, so the preview surface
    cannot be enabled without the lifecycle that produces (and lets the user
    revoke) the likeness it would render: with the avatar flag off there is no
    consent surface, no `ready` profile and therefore no legitimate input. The
    combination was unreachable until V3-017 added the second flag, which is
    why the clause lands with it rather than with V3-013.
    """
    with pytest.raises(
        ValidationError,
        match="ENABLE_V3_STATIC_TRYON requires ENABLE_V3_AVATAR",
    ):
        Settings(
            APP_ENV="dev",
            ENABLE_V3_STATIC_TRYON=True,
            ENABLE_V3_AVATAR=False,
        )


def test_static_tryon_allows_valid_dependency_combination() -> None:
    settings = Settings(
        APP_ENV="dev",
        ENABLE_V3_STATIC_TRYON=True,
        ENABLE_V3_AVATAR=True,
    )

    assert settings.ENABLE_V3_STATIC_TRYON is True


def test_the_avatar_lifecycle_does_not_require_the_preview_surface() -> None:
    """The dependency is one-directional, and that is the shipping default.

    V3-011 through V3-015 are enablable on their own; asserting it here stops a
    later reading of rule 5 from turning the clause into a mutual requirement,
    which would make the avatar features un-shippable without the preview.
    """
    settings = Settings(
        APP_ENV="dev",
        ENABLE_V3_AVATAR=True,
        ENABLE_V3_STATIC_TRYON=False,
    )

    assert settings.ENABLE_V3_AVATAR is True
