"""Pure-function coverage for the grounded reasoning builder (BE#38/#85, no DB)."""

from __future__ import annotations

from typing import Any

from app.application.ports.weather import WeatherInfo
from app.application.recommendations.reasoning import (
    CATEGORY,
    FIT,
    STYLE_MATCH,
    WEAR_HISTORY,
    WEATHER,
    build_reasoning,
)
from app.presentation.api.v1.schemas.recommendations import ReasoningItem


def _weather(temp: float = 15.0, rain: float = 0.0) -> WeatherInfo:
    return WeatherInfo(temperature_c=temp, feels_like_c=temp, rain_probability=rain, condition="晴")


def _types(reasons: list[dict[str, Any]]) -> set[str]:
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


# ---------------------------------------------------------------------------
# BE#85 — source provenance + referenced_item_ids + confidence
# ---------------------------------------------------------------------------


def test_weather_reason_has_weather_rule_source_and_no_item_ids() -> None:
    """Temp-neutral weather reason: source=weather_rule, no specific items."""
    reasons = build_reasoning(
        categories=["top", "bottom"],
        colors=["black", "grey"],
        fit_statuses=["unknown", "unknown"],
        is_fresh=False,
        weather=_weather(temp=15.0),  # neutral temperature → medium-confidence entry
    )
    weather_reasons = [r for r in reasons if r["type"] == WEATHER]
    assert weather_reasons, "expected at least one weather reason"
    for r in weather_reasons:
        assert r["source"] == "weather_rule"
        assert r["referenced_item_ids"] == []


def test_history_reason_carries_wardrobe_history_and_main_item_id() -> None:
    """Freshness reason: source=wardrobe_history, referenced_item_ids=[main_item_id]."""
    main_id = "item-top-uuid"
    reasons = build_reasoning(
        categories=["top", "bottom"],
        colors=["navy", "beige"],
        fit_statuses=["unknown", "unknown"],
        is_fresh=True,
        weather=None,
        item_ids=[main_id, "item-bottom-uuid"],
        main_item_id=main_id,
    )
    history_reasons = [r for r in reasons if r["type"] == WEAR_HISTORY]
    assert history_reasons, "expected a wear_history reason for fresh item"
    r = history_reasons[0]
    assert r["source"] == "wardrobe_history"
    assert r["referenced_item_ids"] == [main_id]
    assert r["confidence"] == "high"


def test_llm_entry_parses_in_schema() -> None:
    """ReasoningItem must accept source=llm + confidence=low (schema-level test).
    This layer doesn't emit llm entries; the schema must still tolerate them for
    forward-compat with any future llm-authored entries written to reasoning_json.
    """
    item = ReasoningItem(
        type="wear_history",
        text="LLM-written reason",
        source="llm",
        confidence="low",
        referenced_item_ids=[],
    )
    assert item.source == "llm"
    assert item.confidence == "low"


def test_old_format_entry_parses_in_schema() -> None:
    """Old persisted rows (type+text only) must not 500 — new fields default to None/[]."""
    item = ReasoningItem(**{"type": "weather", "text": "今天气温适中，整体好驾驭"})
    assert item.source is None
    assert item.referenced_item_ids == []
    assert item.confidence is None


def test_all_entries_have_provenance_fields() -> None:
    """Every entry returned by build_reasoning() carries all BE#85 keys."""
    reasons = build_reasoning(
        categories=["top", "bottom", "outerwear"],
        colors=["beige", "white", "navy"],
        fit_statuses=["fits_well", "unknown", "fits_well"],
        is_fresh=True,
        weather=_weather(temp=5.0),
        preferred_colors={"beige"},
        item_ids=["id-top", "id-bottom", "id-outer"],
        main_item_id="id-top",
    )
    for r in reasons:
        assert "source" in r, f"missing 'source' in {r}"
        assert "referenced_item_ids" in r, f"missing 'referenced_item_ids' in {r}"
        assert "confidence" in r, f"missing 'confidence' in {r}"
        assert isinstance(r["referenced_item_ids"], list)
        assert r["confidence"] in ("low", "medium", "high"), f"bad confidence in {r}"


def test_cold_outerwear_reason_references_outerwear_item() -> None:
    """Cold + outerwear reason cites the outerwear id, not shoes/top."""
    outer_id = "id-outer"
    reasons = build_reasoning(
        categories=["top", "bottom", "outerwear"],
        colors=["white", "black", "navy"],
        fit_statuses=["unknown", "unknown", "unknown"],
        is_fresh=False,
        weather=_weather(temp=5.0),
        item_ids=["id-top", "id-bottom", outer_id],
        main_item_id="id-top",
    )
    cold_reasons = [r for r in reasons if r["type"] == WEATHER and "外层" in r["text"]]
    assert cold_reasons, "expected a cold-weather outerwear reason"
    assert cold_reasons[0]["referenced_item_ids"] == [outer_id]
    assert cold_reasons[0]["confidence"] == "high"
