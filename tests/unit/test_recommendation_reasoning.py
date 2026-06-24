"""Pure-function coverage for the grounded reasoning builder (BE#38, no DB)."""

from __future__ import annotations

from app.application.ports.weather import WeatherInfo
from app.application.recommendations.reasoning import (
    CATEGORY,
    FIT,
    STYLE_MATCH,
    WEAR_HISTORY,
    WEATHER,
    build_reasoning,
)


def _weather(temp: float = 15.0, rain: float = 0.0) -> WeatherInfo:
    return WeatherInfo(temperature_c=temp, feels_like_c=temp, rain_probability=rain, condition="晴")


def _types(reasons: list[dict[str, str]]) -> set[str]:
    return {r["type"] for r in reasons}


def test_cold_with_outerwear_gives_warmth_reason() -> None:
    reasons = build_reasoning(
        categories=["top", "bottom", "outerwear"],
        colors=["black", "grey", "navy"],
        fit_statuses=["unknown", "unknown", "unknown"],
        is_fresh=False,
        weather=_weather(temp=5.0),
    )
    assert WEATHER in _types(reasons)
    assert any("外层" in r["text"] for r in reasons if r["type"] == WEATHER)


def test_cold_without_outerwear_does_not_claim_warmth() -> None:
    # Grounding guard: no outerwear → must NOT assert it keeps you warm.
    reasons = build_reasoning(
        categories=["top", "bottom"],
        colors=["white", "black"],
        fit_statuses=["unknown", "unknown"],
        is_fresh=False,
        weather=_weather(temp=5.0),
    )
    assert not any("外层" in r["text"] for r in reasons)


def test_rain_with_light_shoes_skips_rain_claim() -> None:
    reasons = build_reasoning(
        categories=["top", "bottom", "shoes"],
        colors=["black", "black", "white"],  # white shoes
        fit_statuses=["unknown", "unknown", "unknown"],
        is_fresh=False,
        weather=_weather(rain=0.8),
    )
    assert not any("有雨" in r["text"] for r in reasons)


def test_rain_with_dark_shoes_adds_rain_reason() -> None:
    reasons = build_reasoning(
        categories=["top", "bottom", "shoes"],
        colors=["black", "black", "black"],
        fit_statuses=["unknown", "unknown", "unknown"],
        is_fresh=False,
        weather=_weather(rain=0.8),
    )
    assert any("有雨" in r["text"] for r in reasons)


def test_freshness_and_style_and_fit_reasons() -> None:
    reasons = build_reasoning(
        categories=["top", "bottom"],
        colors=["beige", "white"],
        fit_statuses=["fits_well", "unknown"],
        is_fresh=True,
        weather=None,
        preferred_colors={"beige", "navy"},
    )
    types = _types(reasons)
    assert WEAR_HISTORY in types
    assert STYLE_MATCH in types
    assert FIT in types
    assert CATEGORY in types


def test_always_at_least_two_grounded_reasons() -> None:
    # Minimal context (no weather, not fresh, no prefs) still yields >= 2.
    reasons = build_reasoning(
        categories=["dress"],
        colors=["red"],
        fit_statuses=["unknown"],
        is_fresh=False,
        weather=None,
    )
    assert len(reasons) >= 2
    assert all(r["text"] for r in reasons)


def test_caps_at_max_items() -> None:
    reasons = build_reasoning(
        categories=["top", "bottom", "outerwear"],
        colors=["beige", "white", "navy"],
        fit_statuses=["fits_well", "fits_well", "fits_well"],
        is_fresh=True,
        weather=_weather(temp=5.0),
        preferred_colors={"beige"},
        max_items=4,
    )
    assert len(reasons) <= 4
