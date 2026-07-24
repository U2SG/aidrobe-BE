"""Pure-function coverage for the composable stylist prompt modules (BE#66)."""

from __future__ import annotations

import json

from app.application.ask.answer_question import (
    _extract_structured,
    _extract_trip_intent,
    _recent_reference_clause,
    _strip_ordinal_leaks,
)
from app.application.llm_prompts.assemble import assemble_system_prompt
from app.application.llm_prompts.persona import stylist_persona
from app.application.llm_prompts.preference_memory import render_preferences
from app.application.llm_prompts.reasoning_policy import reasoning_policy
from app.application.llm_prompts.response_contracts import (
    PREFERENCE_TYPE_VOCAB,
    ask_structured_contract,
)
from app.application.llm_prompts.self_critique import (
    disliked_attribute_offenders,
    negative_preference_violations,
    unknown_item_refs,
)


# ── persona ──────────────────────────────────────────────────────────
def test_persona_is_locale_aware_with_zh_default() -> None:
    assert stylist_persona("zh").startswith("你是 Aidrobe")
    assert stylist_persona("en").startswith("You are Aidrobe")
    assert stylist_persona("fr").startswith("你是 Aidrobe")  # unknown -> zh default


def test_recent_reference_clause_positions_only() -> None:
    # Anti-repetition observation: positions land in the clause, nothing else;
    # no positions -> no clause (the variety rule then has no data to act on).
    assert _recent_reference_clause([], "zh") is None
    zh = _recent_reference_clause([1, 4], "zh")
    assert zh is not None and "第 1、4 件" in zh
    en = _recent_reference_clause([2], "en")
    assert en is not None and "items 2" in en


# ── reasoning policy ─────────────────────────────────────────────────
def test_reasoning_policy_names_the_required_dimensions() -> None:
    zh = reasoning_policy("zh")
    en = reasoning_policy("en")
    assert "天气" in zh and "场合" in zh and "偏好" in zh
    for token in ("weather", "occasion", "preferences", "dislikes", "variety"):
        assert token in en
    assert reasoning_policy("fr") == zh  # unknown -> zh default


# ── preference memory rendering ──────────────────────────────────────
def test_render_preferences_returns_none_when_empty() -> None:
    assert render_preferences(None, "zh") is None
    assert render_preferences({}, "zh") is None
    assert render_preferences({"positive": [], "negative": []}, "zh") is None


def test_render_preferences_prioritizes_negatives_first() -> None:
    rendered = render_preferences(
        {
            "positive": [{"type": "color", "value": "navy"}],
            "negative": [{"type": "fit", "value": "oversized"}],
        },
        "en",
    )
    assert rendered is not None
    # MUST AVOID block appears before the PREFER block (priority ordering).
    assert rendered.index("MUST AVOID") < rendered.index("PREFER")
    assert "oversized" in rendered
    assert "navy" in rendered


def test_render_preferences_weak_negative_is_soft_not_hard() -> None:
    # BE#69 review: a low-confidence (feedback-derived) negative must NOT be
    # rendered as a hard "MUST AVOID" rule; a high-confidence one must.
    rendered = render_preferences(
        {
            "negative": [
                {"type": "color", "value": "red", "confidence": 0.9},
                {"type": "fit", "value": "oversized", "confidence": 0.3},
            ]
        },
        "en",
    )
    assert rendered is not None
    assert "MUST AVOID" in rendered and "LEAN AWAY" in rendered
    # The strong one sits under MUST AVOID, the weak one under LEAN AWAY.
    assert rendered.index("MUST AVOID") < rendered.index("red")
    assert rendered.index("LEAN AWAY") < rendered.index("oversized")
    assert rendered.index("red") < rendered.index("LEAN AWAY")


def test_render_preferences_bounds_user_text() -> None:
    # Control chars / newlines (injection vehicle) are stripped, length capped.
    rendered = render_preferences(
        {"negative": [{"type": "style", "value": "bad\nstuff" + "x" * 500}]},
        "zh",
    )
    assert rendered is not None
    assert "badstuff" in rendered  # embedded newline stripped (injection vehicle gone)
    assert "bad\nstuff" not in rendered
    assert "x" * 500 not in rendered  # length capped (sanitize_preference_value)


# ── response contract (centralized) + parser alignment ───────────────
def test_ask_contract_carries_one_canonical_envelope() -> None:
    for lang in ("zh", "en"):
        contract = ask_structured_contract(lang)
        assert contract.count('"answer"') == 1
        for token in (
            '"referenced_items"',
            '"stated_preferences"',
            '"conversation_deltas"',
            '"rejected_focused_item"',
            '"trip_intent"',
            '"duration_days"',
        ):
            assert token in contract
        assert PREFERENCE_TYPE_VOCAB in contract
    assert ask_structured_contract("fr") == ask_structured_contract("zh")


def test_extract_trip_intent_only_surfaces_affirmative_plausible_trips() -> None:
    # BE#115 — missing block, is_trip=false, and malformed shapes all collapse
    # to None; duration survives only as a plausible integer.
    assert _extract_trip_intent({}) is None
    assert _extract_trip_intent({"trip_intent": "5 days"}) is None
    assert _extract_trip_intent({"trip_intent": {"is_trip": False, "duration_days": 5}}) is None
    ok = _extract_trip_intent({"trip_intent": {"is_trip": True, "duration_days": 5}})
    assert ok is not None and ok.is_trip and ok.duration_days == 5
    for bad_duration in (None, "five", True, 0, -3, 365):
        got = _extract_trip_intent({"trip_intent": {"is_trip": True, "duration_days": bad_duration}})
        assert got is not None and got.is_trip and got.duration_days is None


def test_ask_contract_pins_the_answer_style() -> None:
    # Regression for the on-device leak "白色上衣（序号1）": the contract must
    # forbid list positions in the user-facing answer, ban the template
    # opener, and pin the 2–3 sentence structure.
    zh = ask_structured_contract("zh")
    for token in ("自然名称", "严禁出现「序号1」", "根据您的衣橱和偏好", "2–3 句"):
        assert token in zh
    en = ask_structured_contract("en")
    for token in ("never by list position", "template openers", "2–3 sentences"):
        assert token in en


def test_strip_ordinal_leaks_removes_positions_but_keeps_clean_text() -> None:
    assert (
        _strip_ordinal_leaks("推荐白色上衣（序号1）配米色裤子（序号2）。")
        == "推荐白色上衣配米色裤子。"
    )
    assert _strip_ordinal_leaks("选序号3的外套") == "选的外套"
    assert (
        _strip_ordinal_leaks("Wear the white shirt (item 2) with loafers (#5).")
        == "Wear the white shirt with loafers ."
    )
    # Bare forms the prompt also forbids (PR #109 review): 第 N 件 / item N / #N.
    assert _strip_ordinal_leaks("搭配第 2 件白色上衣") == "搭配白色上衣"
    assert _strip_ordinal_leaks("外套选第3件单品即可") == "外套选即可"
    assert _strip_ordinal_leaks("Wear item 2 with the loafers") == "Wear with the loafers"
    assert _strip_ordinal_leaks("Pair #4 with white sneakers") == "Pair with white sneakers"
    # "items" (plural word, not a position) must survive.
    assert _strip_ordinal_leaks("These items 2-in-1 style") == "These items 2-in-1 style"
    clean = "白色上衣配米色长裤，天气热外套拿在手上。"
    assert _strip_ordinal_leaks(clean) == clean


def test_contract_shape_round_trips_through_the_parser() -> None:
    # The contract advertises this exact envelope; the parser must accept it.
    response = json.dumps(
        {
            "answer": "穿这套",
            "referenced_items": [1, 2],
            "stated_preferences": [{"type": "color", "value": "navy", "polarity": "positive"}],
            "conversation_deltas": {
                "reasoning": "r",
                "applied_constraints": ["c"],
                "rejected_focused_item": False,
            },
        }
    )
    parsed = _extract_structured(json.loads(response), item_count=2)
    assert parsed is not None
    answer, indices, prefs = parsed
    assert answer == "穿这套"
    assert indices == [0, 1]
    assert prefs == [{"type": "color", "value": "navy", "polarity": "positive"}]


# ── deterministic self-critique validators ──────────────────────────
def test_unknown_item_refs_flags_hallucinations() -> None:
    assert unknown_item_refs(["a", "b", "c"], {"a", "b"}) == ["c"]
    assert unknown_item_refs(["a"], {"a", "b"}) == []


def test_disliked_attribute_offenders_matches_case_insensitively() -> None:
    items = [
        {"id": "1", "color_main": "Red"},
        {"id": "2", "color_main": "navy"},
    ]
    assert disliked_attribute_offenders(items, "color_main", ["red"]) == ["1"]
    assert disliked_attribute_offenders(items, "color_main", []) == []


def test_negative_preference_violations_maps_types_to_attributes() -> None:
    items = [
        {"id": "1", "color_main": "red", "material": "cotton", "fit_status": "regular"},
        {"id": "2", "color_main": "navy", "material": "linen", "fit_status": "oversized"},
    ]
    negatives = [
        {"type": "color", "value": "red"},
        {"type": "fit", "value": "oversized"},
        {"type": "occasion", "value": "gala"},  # unmapped type -> ignored
    ]
    violations = negative_preference_violations(items, negatives)
    assert any("item 1" in v and "color_main" in v for v in violations)
    assert any("item 2" in v and "fit_status" in v for v in violations)
    assert len(violations) == 2


# ── assembler ────────────────────────────────────────────────────────
def test_assemble_drops_blank_sections_and_preserves_order() -> None:
    out = assemble_system_prompt(["a", None, "  ", "b", ""])
    assert out == "a\n\nb"
