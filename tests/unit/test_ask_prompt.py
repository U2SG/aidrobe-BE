"""Pure-function coverage for the Ask system-prompt builder (no DB/Docker)."""

from __future__ import annotations

import uuid

from app.application.ask.answer_question import (
    _build_system_prompt,
    _parse_structured,
    _preamble_for,
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
    prompt = _build_system_prompt(uuid.uuid4(), _items(), None)
    # Sanitized structured attributes reach the prompt...
    assert '"category": "top"' in prompt
    assert '"color_main": "white"' in prompt
    assert '"category": "bottom"' in prompt
    # ...but free-form titles never do, and the opaque token is attached.
    assert "White shirt" not in prompt
    assert "usr_" in prompt


def test_build_prompt_honors_locale() -> None:
    assert _build_system_prompt(uuid.uuid4(), [], "en").startswith("You are Aidrobe")
    assert _build_system_prompt(uuid.uuid4(), [], "zh-CN").startswith("你是 Aidrobe")


def test_focus_clause_added_only_when_focused() -> None:
    plain = _build_system_prompt(uuid.uuid4(), [], "en", focused=False)
    focused = _build_system_prompt(uuid.uuid4(), [], "en", focused=True)
    assert "FIRST item" not in plain
    assert "FIRST item" in focused
    # Locale-aware: Chinese focus clause for a Chinese locale.
    assert "第 1 件" in _build_system_prompt(uuid.uuid4(), [], "zh-CN", focused=True)


def test_structured_instruction_added_only_when_structured() -> None:
    plain = _build_system_prompt(uuid.uuid4(), [], "en", structured=False)
    structured = _build_system_prompt(uuid.uuid4(), [], "en", structured=True)
    assert "referenced_items" not in plain
    assert "referenced_items" in structured


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
