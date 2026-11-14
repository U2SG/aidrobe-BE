"""Centralized LLM response contracts (BE#66).

Single source of each feature's LLM output JSON contract, so the contract text
lives in one place instead of being duplicated inside feature use cases.

`answer_question._extract_structured` parses exactly the JSON SHAPE
`ask_structured_contract(lang)` advertises, so the envelope keys must not
drift (instruction prose around them may evolve — e.g. the answer-style
rules). `tests/unit/test_llm_prompts.py` locks both the envelope and the
style rules.
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
        '"rejected_focused_item": 仅当用户明确表示不要/换掉当前聚焦的那件单品时为 true，否则为 false}, '
        '"trip_intent": {"is_trip": 仅当用户在为多天出行/旅行准备穿搭或行李时为 true，否则为 false, '
        '"duration_days": 行程天数（整数），用户没说清就为 null}}。'
        "若没有引用任何单品，referenced_items 返回空数组。"
        f"stated_preferences 只收录用户本轮明确表达的长期偏好（如「我不喜欢宽松衬衫」），"
        f"type 必须是以下之一：{PREFERENCE_TYPE_VOCAB}；不要从一次性的请求里推断，没有就返回空数组。"
        "answer 是唯一展示给用户的文字：提及单品只用「白色上衣」这类自然名称，"
        "严禁出现「序号1」「（序号2）」「第 3 件」等列表位置——位置只能写进 referenced_items。"
        "不要用「根据您的衣橱和偏好」这类模板开场；单品卡片会另行展示，不要逐条罗列清单。"
        "结构为：先直接说穿什么，再用一两句讲最贴合当前场合的理由，"
        "必要时补一句天气或舒适度上的调整建议；全文控制在 2–3 句。"
    ),
    "en": (
        "\nRespond with a single JSON object and nothing else: "
        '{"answer": "your advice in English", "referenced_items": [1-based positions '
        'of the items above you reference], '
        '"stated_preferences": [{"type": "...", "value": "short value", "polarity": "positive" or "negative"}], '
        '"conversation_deltas": {"reasoning": "one sentence on how this refines the previous turn", '
        '"applied_constraints": ["temporary constraints applied this turn"], '
        '"rejected_focused_item": true only when the user explicitly rejects / wants to swap out the '
        'focused item, otherwise false}, '
        '"trip_intent": {"is_trip": true only when the user is preparing outfits or packing for a '
        'multi-day trip, otherwise false, "duration_days": the trip length in days as an integer, '
        "or null when unstated}}. "
        "If you reference no item, return an empty array. "
        "stated_preferences captures ONLY durable preferences the user explicitly stated this turn "
        '(e.g. "I dislike oversized shirts"); '
        f"type must be one of: {PREFERENCE_TYPE_VOCAB}. Do not infer from a one-off request; "
        "return an empty array when there are none. "
        "The answer field is the only text the user sees: refer to items by natural name "
        '(like "the white shirt"), never by list position ("item 2", "#3") — positions belong '
        "only in referenced_items. Skip template openers such as \"Based on your wardrobe and "
        'preferences"; the item cards render separately, so do not enumerate the list. '
        "Structure it as: lead with what to wear, follow with one or two sentences of the "
        "reasoning most relevant to the occasion, and add a weather/comfort adjustment only "
        "when useful. Keep the whole answer to 2–3 sentences."
    ),
}


def ask_structured_contract(lang: str) -> str:
    """Return the Ask structured-output JSON contract for a language subtag."""
    return _ASK_STRUCTURED.get(lang, _ASK_STRUCTURED[_DEFAULT_LANG])


_ASK_BUTLER = {
    "zh": (
        "衣橱管家规则。最终 JSON 增加 reply_kind，取值 outfit、item_advice 或 general。"
        "用户要穿什么或要一整套时，只推荐一套。搭配须有上装＋下装或连衣裙，且须有鞋；"
        "在回复前用 validate_outfit 校验全部单品。校验通过才可用 outfit，并把完全相同的序号放进 referenced_items。"
        "部分搭配或单品问题用 item_advice，引用真实可穿单品。两种答复都用单品自然名称。"
        "无可核实的建议时用 general，referenced_items 为空，只提供通用建议或问一个具体问题。"
        "天气或日期影响建议时先用 get_weather，采用用户点名的城市，没点名就省略 place。"
        "只陈述查到的天气，取不到就说明；校验搭配时用同一日期和城市，面试或正式会议用 formal。"
        "衣橱列表不足、问到具体名字或需要查状态时用 search_wardrobe，搜索返回的序号可直接引用。"
        "若聚焦第 1 件单品，搭配需包含它；该件不可穿时请说明，用户明确要求换掉时才推荐替换搭配。"
    ),
    "en": (
        "Wardrobe butler rules. Add reply_kind to the final JSON: outfit, item_advice or general. "
        "For what-to-wear questions, recommend one complete look: top and bottom or a dress, plus shoes. "
        "Call validate_outfit on the exact set first. Use outfit only after it passes; referenced_items must match "
        "the checked positions. Use item_advice for individual pieces or partial pairings, citing wearable pieces. "
        "Use natural item names. Use general with empty referenced_items for general guidance or a clarifying question. "
        "Call get_weather first when weather or a date affects the advice; supply a city only if the user named one. "
        "Report retrieved conditions or say weather is unavailable. Validate using the same date and city, and use "
        "formal for an interview or formal meeting. Use search_wardrobe to find named pieces or check availability; "
        "its positions are valid references. Include the first piece in an item-scoped outfit unless the user "
        "explicitly asks to replace it. Explain when that piece is unavailable."
    ),
}


def ask_butler_contract(lang: str) -> str:
    return _ASK_BUTLER.get(lang, _ASK_BUTLER[_DEFAULT_LANG])
