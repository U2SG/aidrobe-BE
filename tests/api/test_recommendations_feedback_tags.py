"""Tests for A-10: GET /v1/recommendations/feedback-tags"""

from app.application.recommendations.feedback_tags import (
    get_tags_for_scene,
    reload_for_tests,
)


def test_default_only_when_scene_missing() -> None:
    reload_for_tests()
    tags = get_tags_for_scene(None)
    keys = {t.key for t in tags}
    assert "complimented" in keys
    assert "easy_to_move" not in keys


def test_commute_merges_with_default() -> None:
    reload_for_tests()
    tags = get_tags_for_scene("commute")
    keys = [t.key for t in tags]
    assert "easy_to_move" in keys
    assert "presentable" in keys
    assert "complimented" in keys
    assert keys.index("easy_to_move") < keys.index("complimented")


def test_unknown_scene_returns_default() -> None:
    reload_for_tests()
    tags = get_tags_for_scene("not_a_real_scene")
    keys = {t.key for t in tags}
    assert "complimented" in keys
    assert len(tags) > 0


def test_dedup_when_scene_overrides_default_key() -> None:
    reload_for_tests()
    tags = get_tags_for_scene("commute")
    keys = [t.key for t in tags]
    assert len(keys) == len(set(keys)), "duplicate keys after merge"
