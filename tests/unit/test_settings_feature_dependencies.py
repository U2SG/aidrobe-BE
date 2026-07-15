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
