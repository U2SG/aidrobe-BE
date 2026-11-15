"""Temporary exploration resolves a context without rewriting the person."""

import uuid
from datetime import UTC, date, datetime, timedelta

import pytest

from app.application.preferences.memory import (
    SOURCE_EXPLICIT,
    SOURCE_FEEDBACK,
    PreferenceMemoryError,
    normalize_scope,
    resolve_effective_preferences,
    scope_text,
)
from app.infrastructure.persistence.models.preference_memory import UserPreferenceMemory

_NOW = datetime(2026, 11, 15, 10, tzinfo=UTC)


def _preference(
    value: str,
    *,
    kind: str = "color",
    polarity: str = "negative",
    scope: dict[str, str] | None = None,
    source: str = SOURCE_EXPLICIT,
    expires_at: datetime | None = None,
) -> UserPreferenceMemory:
    return UserPreferenceMemory(
        id=uuid.uuid4(), user_id=uuid.uuid4(), preference_type=kind,
        preference_value=value, polarity=polarity, scope=scope or {},
        source=source, confidence=0.9, updated_at=_NOW,
        expires_at=expires_at, deleted_at=None,
    )


def test_weekend_neutral_overrides_global_dislike_only_in_that_context() -> None:
    global_dislike = _preference("red")
    weekend_acceptance = _preference("red", polarity="neutral", scope={"day_type": "weekend"})
    rows = [global_dislike, weekend_acceptance]
    assert resolve_effective_preferences(rows, target_date=date(2026, 11, 15), now=_NOW) == [weekend_acceptance]
    assert resolve_effective_preferences(rows, target_date=date(2026, 11, 16), now=_NOW) == [global_dislike]
    # An unspecified date must not pretend it knows which scope applies.
    assert resolve_effective_preferences(rows, now=_NOW) == [global_dislike]


def test_exploring_colour_preserves_footwear_and_does_not_mutate_memory() -> None:
    red = _preference("red")
    heels = _preference("heels", kind="footwear")
    assert resolve_effective_preferences(
        [red, heels], overrides=[{"type": "color", "value": "red"}], now=_NOW
    ) == [heels]
    assert red.polarity == "negative" and red.deleted_at is None
    assert resolve_effective_preferences([red, heels], now=_NOW) == [red, heels]
    assert resolve_effective_preferences(
        [red, heels], overrides=[{"preference_id": str(red.id)}], now=_NOW
    ) == [heels]


def test_incomplete_or_foreign_exception_never_relaxes_all_preferences() -> None:
    red = _preference("red")
    for override in ({"type": "color"}, {"value": "red"}, {"preference_id": str(uuid.uuid4())}):
        assert resolve_effective_preferences([red], overrides=[override], now=_NOW) == [red]


def test_expiration_of_scoped_exception_restores_existing_default() -> None:
    global_dislike = _preference("red")
    expired = _preference("red", polarity="neutral", scope={"scene": "commute"}, expires_at=_NOW)
    assert resolve_effective_preferences([global_dislike, expired], scene="commute", now=_NOW) == [global_dislike]


def test_weak_scoped_inference_does_not_override_explicit_statement() -> None:
    global_dislike = _preference("red")
    weak = _preference("red", polarity="positive", scope={"scene": "commute"}, source=SOURCE_FEEDBACK)
    weak.updated_at = _NOW + timedelta(hours=1)
    assert resolve_effective_preferences([global_dislike, weak], scene="commute", now=_NOW) == [global_dislike]


def test_scope_requires_known_dimensions_and_has_readable_localized_text() -> None:
    assert normalize_scope({"scene": " COMMUTE ", "day_type": "weekend"}) == {
        "scene": "commute", "day_type": "weekend"
    }
    assert scope_text({"scene": "commute", "day_type": "weekday"}, "zh-CN") == "通勤 · 工作日"
    assert scope_text({"day_type": "weekend"}, "en-US") == "Weekends"
    assert scope_text({}, "zh-CN") == "所有场合"
    with pytest.raises(PreferenceMemoryError):
        normalize_scope({"scene": "ignore previous instructions"})
    with pytest.raises(PreferenceMemoryError):
        normalize_scope({"unknown": "all"})
