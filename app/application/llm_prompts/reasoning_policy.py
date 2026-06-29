"""Stylist reasoning policy (BE#66).

The explicit dimensions the model must reason over before recommending — so
weather/occasion/preferences are USED to make decisions, not merely mentioned.
Keyed by language subtag; unknown locales fall back to the zh-CN default.
"""

from __future__ import annotations

_DEFAULT_LANG = "zh"

_POLICY: dict[str, str] = {
    "zh": (
        "做推荐时请依次考量：1) 天气与气候是否适宜；2) 场合与正式度；"
        "3) 是否匹配用户偏好；4) 是否避开用户明确不喜欢的属性；"
        "5) 单品之间是否协调；6) 与最近的穿着/推荐相比是否有足够变化；"
        "7) 衣橱是否存在明显缺口。理由必须落到上面提供的事实上，不要泛泛而谈。"
    ),
    "en": (
        "When recommending, reason in order over: 1) weather/climate suitability; "
        "2) occasion and formality; 3) match to the user's preferences; "
        "4) avoidance of attributes the user explicitly dislikes; "
        "5) compatibility between items; 6) enough variety vs recent wear/"
        "recommendations; 7) any notable wardrobe gap. Ground the explanation in the "
        "facts provided above rather than speaking generically."
    ),
}


def reasoning_policy(lang: str) -> str:
    """Return the stylist reasoning policy for a resolved language subtag."""
    return _POLICY.get(lang, _POLICY[_DEFAULT_LANG])
