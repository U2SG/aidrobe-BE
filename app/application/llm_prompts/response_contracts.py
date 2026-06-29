"""Centralized LLM response contracts (BE#66).

Single source of each feature's LLM output JSON contract, so the contract text
lives in one place instead of being duplicated inside feature use cases.

`ask_structured_contract(lang)` is byte-for-byte the legacy
`_STRUCTURED_INSTRUCTION` from the Ask use case — `answer_question._extract_structured`
parses exactly this shape, so the text must NOT drift. A snapshot test
(`tests/services/test_llm_prompts.py`) locks it.
"""

from __future__ import annotations

_DEFAULT_LANG = "zh"

# The vocabulary the model may tag a stated preference with — mirrors the write
# service allowlist. Embedded in the prompt so the model emits storable types;
# the write path re-validates, so a bad type is dropped, never persisted.
PREFERENCE_TYPE_VOCAB = (
    "style, color, fit, occasion, brand, material, formality, climate, footwear, negative_preference"
)

# Asks for a strict JSON envelope so the server can validate which items the
# model actually cited. `referenced_items` are 1-based positions into the items
# list. `stated_preferences` captures ONLY durable preferences the user
# explicitly stated this turn (the trusted write path) — never inferences from a
# one-off request; an empty array is the default. `conversation_deltas` (incl.
# `rejected_focused_item`) lives INSIDE this one canonical JSON object so a
# separate trailing instruction can't make the deltas silently vanish.
_ASK_STRUCTURED: dict[str, str] = {
    "zh": (
        "\n请只返回一个 JSON 对象，不要包含任何其它文字："
        '{"answer": "你的中文建议", "referenced_items": [引用到的单品在上面列表中的序号，从 1 开始], '
        '"stated_preferences": [{"type": "类型", "value": "简短的值", "polarity": "positive 或 negative"}], '
        '"conversation_deltas": {"reasoning": "一句话说明如何延续上一轮", '
        '"applied_constraints": ["本轮临时约束"], '
        '"rejected_focused_item": 仅当用户明确表示不要/换掉当前聚焦的那件单品时为 true，否则为 false}}。'
        "若没有引用任何单品，referenced_items 返回空数组。"
        f"stated_preferences 只收录用户本轮明确表达的长期偏好（如「我不喜欢宽松衬衫」），"
        f"type 必须是以下之一：{PREFERENCE_TYPE_VOCAB}；不要从一次性的请求里推断，没有就返回空数组。"
    ),
    "en": (
        "\nRespond with a single JSON object and nothing else: "
        '{"answer": "your advice in English", "referenced_items": [1-based positions '
        'of the items above you reference], '
        '"stated_preferences": [{"type": "...", "value": "short value", "polarity": "positive" or "negative"}], '
        '"conversation_deltas": {"reasoning": "one sentence on how this refines the previous turn", '
        '"applied_constraints": ["temporary constraints applied this turn"], '
        '"rejected_focused_item": true only when the user explicitly rejects / wants to swap out the '
        'focused item, otherwise false}}. '
        "If you reference no item, return an empty array. "
        "stated_preferences captures ONLY durable preferences the user explicitly stated this turn "
        '(e.g. "I dislike oversized shirts"); '
        f"type must be one of: {PREFERENCE_TYPE_VOCAB}. Do not infer from a one-off request; "
        "return an empty array when there are none."
    ),
}


def ask_structured_contract(lang: str) -> str:
    """Return the Ask structured-output JSON contract for a language subtag."""
    return _ASK_STRUCTURED.get(lang, _ASK_STRUCTURED[_DEFAULT_LANG])
