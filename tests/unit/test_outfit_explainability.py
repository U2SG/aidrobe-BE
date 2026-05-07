"""Phase B unit tests: scoring (static + dynamic), obstacles, prep.
Pure functions over synthetic WardrobeItem instances — no DB."""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest

from app.application.outfits.obstacles import detect_obstacles, has_dirty
from app.application.outfits.prep import compute_prep
from app.application.outfits.scoring.dynamic import compute_dynamic_scores
from app.application.outfits.scoring.rules import reload_for_tests
from app.application.outfits.scoring.static import compute_static_breakdown
from app.infrastructure.persistence.models.wardrobe import WardrobeItem


def _item(
    *,
    title: str = "T",
    category: str = "shirt",
    color: str = "black",
    status: str = "wearable",
    fit_status: str = "fits_well",
    formality_score: int | None = 50,
    season_tags: list[str] | None = None,
    material: str | None = None,
    location: str | None = None,
    last_worn_at: date | None = None,
) -> WardrobeItem:
    item = WardrobeItem()
    item.id = uuid.uuid4()
    item.user_id = uuid.uuid4()
    item.title = title
    item.category = category
    item.color_main = color
    item.status = status
    item.fit_status = fit_status
    item.formality_score = formality_score
    item.season_tags = {"tags": season_tags} if season_tags is not None else None
    item.material = material
    item.location = location
    item.last_worn_at = last_worn_at
    item.wear_count = 0
    return item


@pytest.fixture(autouse=True)
def _clear_rule_cache() -> None:
    reload_for_tests()


# ----- static layer -----

def test_static_color_harmony_two_distinct_is_perfect() -> None:
    items = [_item(color="black"), _item(color="white")]
    out = compute_static_breakdown(items, outfit_type="casual")
    assert out["aesthetic"]["components"]["color_harmony"] == 1.0


def test_static_color_harmony_four_distinct_drops() -> None:
    items = [_item(color=c) for c in ("black", "white", "red", "blue")]
    out = compute_static_breakdown(items, outfit_type="casual")
    assert out["aesthetic"]["components"]["color_harmony"] == 0.4


def test_static_formality_match_formal_outfit_likes_high_score() -> None:
    items = [_item(formality_score=80), _item(formality_score=90)]
    out = compute_static_breakdown(items, outfit_type="formal")
    assert out["aesthetic"]["components"]["formality_match"] >= 0.8


def test_static_no_formality_score_does_not_penalize() -> None:
    items = [_item(formality_score=None), _item(formality_score=None)]
    out = compute_static_breakdown(items, outfit_type="formal")
    # Falls back to 0.7 neutral, not 0.
    assert out["aesthetic"]["components"]["formality_match"] == 0.7


def test_static_season_fit_matches_current() -> None:
    items = [_item(season_tags=["spring"]), _item(season_tags=["all_season"])]
    out = compute_static_breakdown(items, outfit_type="casual", current_season="spring")
    assert out["aesthetic"]["components"]["season_fit"] == 1.0


# ----- dynamic layer -----

def test_dynamic_feasibility_zero_when_any_dirty() -> None:
    items = [_item(status="wearable"), _item(status="dirty")]
    scores = compute_dynamic_scores(items, static_breakdown=None)
    assert scores.feasibility == 0.0


def test_dynamic_fit_falls_back_when_status_unknown() -> None:
    items = [_item(fit_status="unknown"), _item(fit_status="unknown")]
    scores = compute_dynamic_scores(items, static_breakdown=None)
    # Map says unknown=0.5
    assert scores.fit == 0.5


def test_dynamic_total_clamped_to_unit_interval() -> None:
    items = [_item(fit_status="fits_well")]
    scores = compute_dynamic_scores(
        items,
        static_breakdown={"aesthetic": {"value": 1.0}, "prep_minutes_base": 1},
    )
    assert 0.0 <= scores.total <= 1.0


def test_dynamic_degrades_when_no_static_breakdown() -> None:
    items = [_item()]
    scores = compute_dynamic_scores(items, static_breakdown=None)
    # aesthetic falls back to 0.5
    assert scores.aesthetic == 0.5


def test_dynamic_prep_minutes_picks_up_modifiers() -> None:
    items = [
        _item(location="衣柜A", material="silk"),
        _item(location="衣柜B"),
    ]
    scores = compute_dynamic_scores(
        items,
        static_breakdown={"aesthetic": {"value": 0.8}, "prep_minutes_base": 2},
        has_dirty_obstacle=False,
    )
    # base 2 + 0.5 (extra location) + 1.5 (silk) = 4
    assert scores.prep_minutes == 4


# ----- obstacles -----

def test_obstacle_dirty_is_blocking() -> None:
    items = [_item(status="dirty", title="黑色西裤")]
    obs = detect_obstacles(items, today=date(2026, 5, 7))
    assert len(obs) == 1
    assert obs[0]["kind"] == "laundry"
    assert obs[0]["severity"] == "blocking"
    assert "黑色西裤" in obs[0]["title"]["fallback_text"]


def test_obstacle_lost_takes_precedence_over_seasonal() -> None:
    items = [_item(status="lost", season_tags=["winter"])]
    obs = detect_obstacles(items, today=date(2026, 5, 7), current_season="spring")
    assert len(obs) == 1
    assert obs[0]["kind"] == "lost"


def test_obstacle_seasonal_when_tags_dont_include_current() -> None:
    items = [_item(season_tags=["winter"], title="羊毛大衣")]
    obs = detect_obstacles(items, today=date(2026, 5, 7), current_season="summer")
    assert len(obs) == 1
    assert obs[0]["kind"] == "seasonal"
    assert obs[0]["severity"] == "warning"


def test_obstacle_stale_after_threshold() -> None:
    long_ago = date(2026, 5, 7) - timedelta(days=120)
    items = [_item(last_worn_at=long_ago, title="夏装连衣裙", season_tags=["summer"])]
    obs = detect_obstacles(items, today=date(2026, 5, 7), current_season="summer")
    assert len(obs) == 1
    assert obs[0]["kind"] == "stale"
    assert obs[0]["title"]["params"]["days"] == 120


def test_obstacle_no_season_tags_does_not_trigger_seasonal() -> None:
    items = [_item(season_tags=None)]
    obs = detect_obstacles(items, today=date(2026, 5, 7), current_season="spring")
    assert obs == []


def test_obstacle_empty_string_season_does_not_trigger_seasonal() -> None:
    # Regression: empty-string season must not flag every non-all_season item.
    items = [_item(season_tags=["winter"])]
    obs = detect_obstacles(items, today=date(2026, 5, 7), current_season="")
    assert obs == []


def test_has_dirty_helper() -> None:
    obs = [{"kind": "laundry", "severity": "blocking"}]
    assert has_dirty(obs) is True
    assert has_dirty([]) is False


# ----- prep -----

def test_prep_groups_by_location() -> None:
    items = [
        _item(title="衬衫", location="衣柜A"),
        _item(title="西裤", location="衣柜A"),
        _item(title="皮鞋", location="鞋柜"),
    ]
    out = compute_prep(items)
    assert len(out["steps"]) == 2
    fallback_texts = [s["fallback_text"] for s in out["steps"]]
    # "衣柜A" group merges 衬衫 + 西裤
    assert any("衣柜A" in t and "衬衫" in t and "西裤" in t for t in fallback_texts)
    assert any("鞋柜" in t and "皮鞋" in t for t in fallback_texts)


def test_prep_no_location_falls_into_fetch_unknown() -> None:
    items = [_item(title="围巾", location=None)]
    out = compute_prep(items)
    assert len(out["steps"]) == 1
    assert out["steps"][0]["key"] == "prep.fetch_unknown"


def test_prep_material_notes_for_silk() -> None:
    items = [_item(title="真丝衬衫", material="silk", location="衣柜A")]
    out = compute_prep(items)
    assert len(out["notes"]) == 1
    assert out["notes"][0]["key"] == "prep.material.silk"
    assert "真丝衬衫" in out["notes"][0]["fallback_text"]


def test_prep_no_notes_when_all_robust_materials() -> None:
    items = [_item(title="棉T", material="cotton", location="衣柜A")]
    out = compute_prep(items)
    assert out["notes"] == []
