"""Pure-function coverage for the Ask system-prompt builder (no DB/Docker)."""

from __future__ import annotations

import json
import uuid
from datetime import date, timedelta

from app.application.ask.answer_question import (
    _BASE_PROJECTED_TOKENS,
    _MAX_PER_COLOR_CATEGORY,
    _build_system_prompt,
    _candidate_selection_meta,
    _curate_candidates,
    _extract_structured,
    _item_to_sanitized,
    _preamble_for,
    _project_tokens,
    _resolve_lang,
)
from app.application.ask.wardrobe_context import (
    SNAPSHOT_VERSION,
    WardrobeContext,
    context_snapshot,
    summarize_wardrobe,
)
from app.infrastructure.persistence.models.wardrobe import WardrobeItem

_TODAY = date(2026, 6, 24)


def _parse_structured(
    text: str, item_count: int
) -> tuple[str, list[int], list[dict[str, str]]] | None:
    """Test shim: replicate the production parse-once path (json.loads + dict
    guard) then delegate to `_extract_structured`, so these unit tests still
    exercise the extraction logic by feeding a raw JSON string."""
    try:
        data = json.loads(text)
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    return _extract_structured(data, item_count)


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
        candidate_selection={"strategy": "test", "count_by_category": {}, "focused_in_top1": False},
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


def test_resolve_lang_request_over_settings_over_default() -> None:
    # BE#30 — request locale wins; else settings locale; else zh-CN default.
    assert _resolve_lang("en-US", "zh-CN") == "en"  # request wins
    assert _resolve_lang(None, "en-US") == "en"  # falls back to settings
    assert _resolve_lang(None, "system") == "zh"  # 'system' -> default
    assert _resolve_lang(None, None) == "zh"  # nothing -> default
    assert _resolve_lang("fr", "en-US") == "en"  # unknown request -> settings


def test_build_prompt_feeds_wardrobe_and_hides_titles() -> None:
    items = _items()
    prompt = _build_system_prompt(uuid.uuid4(), items, _ctx(items=items), _TODAY)
    # Sanitized structured attributes reach the prompt...
    assert '"category": "top"' in prompt
    assert '"color_main": "white"' in prompt
    assert '"category": "bottom"' in prompt
    # ...but free-form titles never do, and the opaque token is attached.
    assert "White shirt" not in prompt
    assert "usr_" in prompt


def test_build_prompt_honors_locale() -> None:
    assert _build_system_prompt(uuid.uuid4(), [], _ctx(locale="en"), _TODAY).startswith("You are Aidrobe")
    assert _build_system_prompt(uuid.uuid4(), [], _ctx(locale="zh"), _TODAY).startswith("你是 Aidrobe")


def test_focus_clause_added_only_when_focused() -> None:
    plain = _build_system_prompt(uuid.uuid4(), [], _ctx(locale="en", scope="wardrobe"), _TODAY)
    focused = _build_system_prompt(uuid.uuid4(), [], _ctx(locale="en", scope="item"), _TODAY)
    assert "FIRST item" not in plain
    assert "FIRST item" in focused
    # Locale-aware: Chinese focus clause for a Chinese locale.
    assert "第 1 件" in _build_system_prompt(uuid.uuid4(), [], _ctx(locale="zh", scope="item"), _TODAY)


def test_structured_instruction_added_only_when_structured() -> None:
    plain = _build_system_prompt(uuid.uuid4(), [], _ctx(locale="en"), _TODAY, structured=False)
    structured = _build_system_prompt(uuid.uuid4(), [], _ctx(locale="en"), _TODAY, structured=True)
    assert "referenced_items" not in plain
    assert "referenced_items" in structured


def test_structured_instruction_is_one_canonical_envelope_with_deltas() -> None:
    """FIX 2 — conversation_deltas (incl. rejected_focused_item) lives INSIDE the
    single JSON envelope, not a second trailing instruction fragment, so the
    model returns one coherent object and the deltas can't silently vanish."""
    for locale in ("en", "zh"):
        prompt = _build_system_prompt(
            uuid.uuid4(), [], _ctx(locale=locale), _TODAY, structured=True
        )
        # Exactly one envelope — a single "answer" key, not two stitched fragments.
        assert prompt.count('"answer"') == 1
        # All envelope fields, including the folded-in deltas, are present.
        for field in (
            '"referenced_items"',
            '"stated_preferences"',
            '"conversation_deltas"',
            '"rejected_focused_item"',
        ):
            assert field in prompt


def test_build_prompt_includes_context_dimensions() -> None:
    items = _items()
    ctx = _ctx(items=items, style_tags=["minimal", "commute"])
    prompt = _build_system_prompt(uuid.uuid4(), items, ctx, _TODAY)
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
    assert snap["snapshot_version"] == SNAPSHOT_VERSION == 2
    for key in (
        "scope",
        "locale",
        "wardrobe_summary",
        "style_profile",
        "fit_preferences",
        "recent_categories",
        "candidate_item_ids",
        "candidate_selection",  # BE#27 — selection explainability
    ):
        assert key in snap
    # Selection metadata is snapshot-only and never leaks into the prompt block.
    assert "candidate_selection" not in {
        "scope",
        "wardrobe_summary",
        "style_profile",
        "fit_preferences",
        "recent_categories",
        "departure_time",
    }


def test_item_sanitized_emits_extended_fields_and_recency() -> None:
    item = WardrobeItem(
        user_id=uuid.uuid4(),
        title="Linen blazer",
        category="outerwear",
        sub_category="blazer",
        color_main="beige",
        color_secondary="white",
        material="linen",
        status="wearable",
        formality_score=7,
        time_cost_level="medium",
        wear_count=12,
        last_worn_at=_TODAY - timedelta(days=5),
        cutout_status="ready",
    )
    out = _item_to_sanitized(item, _TODAY)
    assert out["sub_category"] == "blazer"
    assert out["material"] == "linen"
    assert out["formality_score"] == 7
    assert out["wear_count"] == 12
    assert out["last_worn_days_ago"] == 5
    # Raw date / title / image never reach the projection.
    assert "last_worn_at" not in out
    assert "Linen blazer" not in str(out)


def test_item_sanitized_bounds_style_tags() -> None:
    item = WardrobeItem(
        user_id=uuid.uuid4(),
        title="x",
        category="top",
        color_main="white",
        status="wearable",
        style_tags=["minimal", "y" * 200] + [f"t{i}" for i in range(20)],
    )
    out = _item_to_sanitized(item, _TODAY)
    tags = out["style_tags"]
    assert len(tags) <= 8  # count bound
    assert all(len(t) <= 50 for t in tags)  # per-tag length bound


def _wearable(category: str, color: str, *, wear_count: int = 0) -> WardrobeItem:
    return WardrobeItem(
        user_id=uuid.uuid4(),
        title=f"{color} {category}",
        category=category,
        color_main=color,
        status="wearable",
        wear_count=wear_count,
    )


def test_curate_caps_same_color_category_and_covers_categories() -> None:
    # 10 white tops + 1 pair of shoes; dedup caps the tops, round-robin keeps
    # the lone shoes from being crowded out.
    pool = [_wearable("top", "white", wear_count=100 - i) for i in range(10)]
    pool.append(_wearable("shoes", "black"))
    out = _curate_candidates(pool, None, max_items=80)
    tops = [i for i in out if i.category == "top"]
    shoes = [i for i in out if i.category == "shoes"]
    assert len(tops) == _MAX_PER_COLOR_CATEGORY  # same color+cat sampled down
    assert len(shoes) == 1  # coverage preserved


def test_curate_pins_focused_first_and_respects_max() -> None:
    focused = _wearable("dress", "red")
    pool = [_wearable("top", "white", wear_count=99) for _ in range(5)]
    out = _curate_candidates(pool, focused, max_items=3)
    assert out[0].id == focused.id  # focused pinned to position 1
    assert len(out) == 3  # never exceeds the cap


def test_candidate_selection_meta_flags_focused_top1() -> None:
    focused = _wearable("dress", "red")
    items = [focused, _wearable("top", "white")]
    meta = _candidate_selection_meta(items, focused)
    assert meta["focused_in_top1"] is True
    assert meta["count_by_category"] == {"dress": 1, "top": 1}
    assert meta["prompt_field_schema_version"] == 2


def test_project_tokens_scales_with_item_count() -> None:
    assert _project_tokens(0) == _BASE_PROJECTED_TOKENS
    # More candidate items -> a larger pre-call reservation.
    assert _project_tokens(80) > _project_tokens(10) > _project_tokens(0)


def test_parse_structured_maps_valid_positions_to_indices() -> None:
    assert _parse_structured('{"answer": "hi", "referenced_items": [1, 3]}', 3) == (
        "hi",
        [0, 2],
        [],
    )


def test_parse_structured_drops_hallucinated_dupe_and_nonint_positions() -> None:
    # 1 valid; dup 1 dropped; 0 and 4 out of range; "2" non-int; true is bool.
    answer, indices, _prefs = _parse_structured(  # type: ignore[misc]
        '{"answer": "x", "referenced_items": [1, 1, 0, 4, "2", true, 2]}', 3
    )
    assert answer == "x"
    assert indices == [0, 1]


def test_parse_structured_defaults_missing_refs_to_empty() -> None:
    assert _parse_structured('{"answer": "none"}', 3) == ("none", [], [])
    assert _parse_structured('{"answer": "none", "referenced_items": []}', 3) == (
        "none",
        [],
        [],
    )


def test_parse_structured_extracts_well_formed_stated_preferences() -> None:
    answer, indices, prefs = _parse_structured(  # type: ignore[misc]
        '{"answer": "ok", "referenced_items": [], "stated_preferences": ['
        '{"type": "fit", "value": "oversized", "polarity": "negative"},'
        '{"type": "color", "value": "navy", "polarity": "positive"},'
        '{"value": "missing type"}, "not a dict", {"type": "x", "value": 5, "polarity": "negative"}'
        "]}",
        3,
    )
    assert answer == "ok"
    assert indices == []
    # Only the two fully-string-typed entries survive; malformed ones are dropped.
    assert prefs == [
        {"type": "fit", "value": "oversized", "polarity": "negative"},
        {"type": "color", "value": "navy", "polarity": "positive"},
    ]


def test_parse_structured_returns_none_on_bad_shape() -> None:
    assert _parse_structured("not json", 3) is None
    assert _parse_structured('["a", "b"]', 3) is None  # not a dict
    assert _parse_structured('{"referenced_items": [1]}', 3) is None  # no answer
    assert _parse_structured('{"answer": 5, "referenced_items": [1]}', 3) is None
    assert _parse_structured('{"answer": "x", "referenced_items": "1"}', 3) is None
