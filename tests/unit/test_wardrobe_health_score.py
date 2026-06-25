"""Pins the wardrobe-health score formula so future tweaks are reasoned about.

Pure unit test — no DB / Docker needed. The score has exactly two terms:
    100 - (1 - coverage_ratio) * 75 - max(0, dominant_ratio - 0.5) * 50
both clamped to [0, 100]. The single 75 coverage weight replaces the old
double-counted (1 - coverage) * 40 + unused_90_ratio * 35, which were
complements over the same 90-day window.
"""

from app.application.insights.wardrobe_health import _COVERAGE_WEIGHT, _score


def test_coverage_weight_is_single_explicit_constant() -> None:
    assert _COVERAGE_WEIGHT == 75.0


def test_perfect_coverage_balanced_is_full_score() -> None:
    assert _score(coverage_ratio=1.0, dominant_ratio=0.5) == 100


def test_zero_coverage_only_costs_the_coverage_weight() -> None:
    # 100 - 1*75 - 0 = 25
    assert _score(coverage_ratio=0.0, dominant_ratio=0.5) == 25


def test_dominance_penalty_above_half() -> None:
    # coverage 1.0 → no coverage cost; dominance 0.6 → (0.6-0.5)*50 = 5
    assert _score(coverage_ratio=1.0, dominant_ratio=0.6) == 95


def test_dominance_capped_at_25_for_single_category() -> None:
    # ratio 1.0 → (1.0-0.5)*50 = 25
    assert _score(coverage_ratio=1.0, dominant_ratio=1.0) == 75


def test_seeded_fixture_scenario_scores_50() -> None:
    # Mirrors the API fixture: coverage 0.4, dominant 0.6.
    # 100 - 0.6*75 - 0.1*50 = 100 - 45 - 5 = 50
    assert _score(coverage_ratio=0.4, dominant_ratio=0.6) == 50


def test_score_is_clamped_to_zero_floor() -> None:
    # Worst case: no coverage (-75) + full dominance (-25) = 100-75-25 = 0 floor.
    assert _score(coverage_ratio=0.0, dominant_ratio=1.0) == 0
    # No coverage but balanced (no dominance penalty): 100-75-0 = 25, still >= 0.
    assert _score(coverage_ratio=0.0, dominant_ratio=0.0) == 25
