"""BE#82 — indoor-climate inference + its ranking/reason effects (pure, no DB)."""

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.application.recommendations.engine import EngineItem, RecommendationEngine
from app.application.recommendations.indoor_climate import (
    INDOOR_COLD,
    INDOOR_WARM,
    IndoorClimateSignal,
    infer_indoor_climate,
)
from app.application.recommendations.reasoning import build_reasoning
from app.common.enums import Category, RecommendationEventType

_NOW = datetime(2026, 7, 10, 12, 0, tzinfo=UTC)


def _mismatch(reason: str, *, days_ago: int = 0) -> SimpleNamespace:
    return SimpleNamespace(
        event_type=RecommendationEventType.TEMPERATURE_MISMATCH_REPORTED.value,
        event_metadata={"reason": reason},
        created_at=_NOW - timedelta(days=days_ago),
    )


# --- inference ---------------------------------------------------------------

def test_repeated_too_cold_infers_indoor_cold() -> None:
    events = [_mismatch("too_cold") for _ in range(3)]
    signal = infer_indoor_climate(events, None, now=_NOW)
    assert signal is not None
    assert signal.tendency == INDOOR_COLD
    assert signal.confidence == "medium"


def test_single_mismatch_does_not_overfit() -> None:
    # AC: one-off must not become a tendency.
    signal = infer_indoor_climate([_mismatch("too_cold")], None, now=_NOW)
    assert signal is None


def test_less_outdoor_lowers_the_bar_to_two() -> None:
    events = [_mismatch("too_warm"), _mismatch("too_warm")]
    # default (more_outdoor/unknown) needs 3 → no signal
    assert infer_indoor_climate(events, None, now=_NOW) is None
    # less_outdoor is indoor-dominant → 2 is enough
    signal = infer_indoor_climate(events, "less_outdoor", now=_NOW)
    assert signal is not None
    assert signal.tendency == INDOOR_WARM


def test_mixed_noise_does_not_infer() -> None:
    events = [_mismatch("too_cold"), _mismatch("too_warm"), _mismatch("too_cold")]
    # cold=2, warm=1, threshold 3 (default) → neither clears → None
    assert infer_indoor_climate(events, None, now=_NOW) is None


def test_stale_events_outside_window_ignored() -> None:
    events = [_mismatch("too_cold", days_ago=40) for _ in range(3)]
    assert infer_indoor_climate(events, "less_outdoor", now=_NOW) is None


def test_non_mismatch_events_ignored() -> None:
    events = [
        SimpleNamespace(
            event_type=RecommendationEventType.OUTFIT_IMPRESSION.value,
            event_metadata=None,
            created_at=_NOW,
        )
    ]
    assert infer_indoor_climate(events, "less_outdoor", now=_NOW) is None


# --- reason (build_reasoning) ------------------------------------------------

def _reason_kwargs(categories: list[str]) -> dict:
    return {
        "categories": categories,
        "colors": ["black"] * len(categories),
        "fit_statuses": ["unknown"] * len(categories),
        "is_fresh": False,
        "weather": None,
        "item_ids": [f"i{i}" for i in range(len(categories))],
    }


def test_reason_emitted_only_with_outerwear_and_never_labels_user() -> None:
    cats = [Category.TOP.value, Category.BOTTOM.value, Category.OUTERWEAR.value]
    reasons = build_reasoning(**_reason_kwargs(cats), indoor_climate=INDOOR_COLD)
    indoor = [r for r in reasons if r["source"] == "indoor_climate_inference"]
    assert len(indoor) == 1
    assert indoor[0]["confidence"] == "medium"
    # never expose an internal label
    assert "室内通勤" not in indoor[0]["text"]
    assert "用户" not in indoor[0]["text"]


def test_reason_absent_without_outerwear() -> None:
    cats = [Category.TOP.value, Category.BOTTOM.value]
    reasons = build_reasoning(**_reason_kwargs(cats), indoor_climate=INDOOR_COLD)
    assert not [r for r in reasons if r["source"] == "indoor_climate_inference"]


def test_reason_absent_when_no_inference() -> None:
    cats = [Category.TOP.value, Category.OUTERWEAR.value]
    reasons = build_reasoning(**_reason_kwargs(cats), indoor_climate=None)
    assert not [r for r in reasons if r["source"] == "indoor_climate_inference"]


# --- ranking bonus (engine._indoor_climate_score) ----------------------------

def _item(category: str) -> EngineItem:
    return EngineItem(item_id=category, title=category, category=category, color_main="black")


def test_ranking_bonus_prefers_removable_outer_layer() -> None:
    engine = RecommendationEngine()
    signal = IndoorClimateSignal(tendency=INDOOR_COLD)
    with_outer = [_item(Category.TOP.value), _item(Category.OUTERWEAR.value)]
    without_outer = [_item(Category.TOP.value), _item(Category.BOTTOM.value)]
    assert engine._indoor_climate_score(with_outer, signal) > 0
    assert engine._indoor_climate_score(without_outer, signal) == 0.0


def test_ranking_bonus_zero_when_flag_off() -> None:
    engine = RecommendationEngine()
    with_outer = [_item(Category.TOP.value), _item(Category.OUTERWEAR.value)]
    # None (flag off / no inference) → 0.0, byte-identical to legacy.
    assert engine._indoor_climate_score(with_outer, None) == 0.0
