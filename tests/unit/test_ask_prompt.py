"""Pure-function coverage for the Ask system-prompt builder (no DB/Docker)."""

from __future__ import annotations

import uuid

from app.application.ask.answer_question import (
    _BASE_PROJECTED_TOKENS,
    _build_system_prompt,
    _parse_structured,
    _preamble_for,
    _project_tokens,
)
from app.application.ask.wardrobe_context import (
    WardrobeContext,
    context_snapshot,
    summarize_wardrobe,
)
from app.infrastructure.persistence.models.wardrobe import WardrobeItem


def _items() -> list[WardrobeItem]:
    return [
        WardrobeItem(
            user_id=uuid.uuid4(),
            title="White shirt",
            category="top",
            color_main="white",
            status="wearable",
            season_tags=["summer"],
            location="home",
        ),
        WardrobeItem(
            user_id=uuid.uuid4(),
            title="Black pants",
            category="bottom",
            color_main="black",
            status="wearable",
        ),
    ]


def _ctx(
    *,
    locale: str = "zh",
    scope: str = "wardrobe",
    items: list[WardrobeItem] | None = None,
    style_tags: list[str] | None = None,
) -> WardrobeContext:
    items = items or []
    return WardrobeContext(
        scope=scope,
        focused_item_id=None,
        locale=locale,
        wardrobe_summary=summarize_wardrobe(items),
        style_profile={"style_tags": style_tags or [], "preferred_colors": []},
        fit_preferences={},
        recent_categories=[],
        departure_time=None,
        candidate_item_ids=[str(i.id) for i in items],
    )


def test_preamble_defaults_to_chinese() -> None:
    assert _preamble_for(None).startswith("你是 Aidrobe")
    assert _preamble_for("").startswith("你是 Aidrobe")
    assert _preamble_for("fr").startswith("你是 Aidrobe")  # unknown -> default


def test_preamble_matches_language_subtag() -> None:
    assert _preamble_for("en").startswith("You are Aidrobe")
    assert _preamble_for("en-US").startswith("You are Aidrobe")
    assert _preamble_for("EN").startswith("You are Aidrobe")
    assert _preamble_for("zh-CN").startswith("你是 Aidrobe")


def test_build_prompt_feeds_wardrobe_and_hides_titles() -> None:
    items = _items()
    prompt = _build_system_prompt(uuid.uuid4(), items, _ctx(items=items))
    # Sanitized structured attributes reach the prompt...
    assert '"category": "top"' in prompt
    assert '"color_main": "white"' in prompt
    assert '"category": "bottom"' in prompt
    # ...but free-form titles never do, and the opaque token is attached.
    assert "White shirt" not in prompt
    assert "usr_" in prompt


def test_build_prompt_honors_locale() -> None:
    assert _build_system_prompt(uuid.uuid4(), [], _ctx(locale="en")).startswith(
        "You are Aidrobe"
    )
    assert _build_system_prompt(uuid.uuid4(), [], _ctx(locale="zh")).startswith(
        "你是 Aidrobe"
    )


def test_focus_clause_added_only_when_focused() -> None:
    plain = _build_system_prompt(uuid.uuid4(), [], _ctx(locale="en", scope="wardrobe"))
    focused = _build_system_prompt(uuid.uuid4(), [], _ctx(locale="en", scope="item"))
    assert "FIRST item" not in plain
    assert "FIRST item" in focused
    # Locale-aware: Chinese focus clause for a Chinese locale.
    assert "第 1 件" in _build_system_prompt(
        uuid.uuid4(), [], _ctx(locale="zh", scope="item")
    )


def test_structured_instruction_added_only_when_structured() -> None:
    plain = _build_system_prompt(uuid.uuid4(), [], _ctx(locale="en"), structured=False)
    structured = _build_system_prompt(
        uuid.uuid4(), [], _ctx(locale="en"), structured=True
    )
    assert "referenced_items" not in plain
    assert "referenced_items" in structured


def test_build_prompt_includes_context_dimensions() -> None:
    items = _items()
    ctx = _ctx(items=items, style_tags=["minimal", "commute"])
    prompt = _build_system_prompt(uuid.uuid4(), items, ctx)
    # Each context dimension actually reaches the prompt (regression guard
    # against silently dropping context like the original double-sanitize bug).
    assert '"wardrobe_summary"' in prompt
    assert '"style_profile"' in prompt
    assert "minimal" in prompt  # style tag flows through
    assert '"fit_preferences"' in prompt
    assert '"recent_categories"' in prompt
    assert '"scope"' in prompt


def test_summarize_wardrobe_counts_by_category() -> None:
    summary = summarize_wardrobe(_items())
    assert summary["total"] == 2
    assert summary["by_category"] == {"top": 1, "bottom": 1}


def test_context_snapshot_is_versioned_and_complete() -> None:
    snap = context_snapshot(_ctx(items=_items()))
    assert snap["snapshot_version"] == 1
    for key in (
        "scope",
        "locale",
        "wardrobe_summary",
        "style_profile",
        "fit_preferences",
        "recent_categories",
        "candidate_item_ids",
    ):
        assert key in snap


def test_project_tokens_scales_with_item_count() -> None:
    assert _project_tokens(0) == _BASE_PROJECTED_TOKENS
    # More candidate items -> a larger pre-call reservation.
    assert _project_tokens(80) > _project_tokens(10) > _project_tokens(0)


def test_parse_structured_maps_valid_positions_to_indices() -> None:
    assert _parse_structured('{"answer": "hi", "referenced_items": [1, 3]}', 3) == (
        "hi",
        [0, 2],
    )


def test_parse_structured_drops_hallucinated_dupe_and_nonint_positions() -> None:
    # 1 valid; dup 1 dropped; 0 and 4 out of range; "2" non-int; true is bool.
    answer, indices = _parse_structured(  # type: ignore[misc]
        '{"answer": "x", "referenced_items": [1, 1, 0, 4, "2", true, 2]}', 3
    )
    assert answer == "x"
    assert indices == [0, 1]


def test_parse_structured_defaults_missing_refs_to_empty() -> None:
    assert _parse_structured('{"answer": "none"}', 3) == ("none", [])
    assert _parse_structured('{"answer": "none", "referenced_items": []}', 3) == (
        "none",
        [],
    )


def test_parse_structured_returns_none_on_bad_shape() -> None:
    assert _parse_structured("not json", 3) is None
    assert _parse_structured('["a", "b"]', 3) is None  # not a dict
    assert _parse_structured('{"referenced_items": [1]}', 3) is None  # no answer
    assert _parse_structured('{"answer": 5, "referenced_items": [1]}', 3) is None
    assert _parse_structured('{"answer": "x", "referenced_items": "1"}', 3) is None
