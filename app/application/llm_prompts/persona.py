"""Shared stylist persona (BE#66).

One consistent voice across Ask + Today (and Should-I-Buy when it grows an LLM
call), so the product reads as a single personal stylist rather than separate
feature-specific calls. Keyed by language subtag ("zh"/"en"); unknown locales
fall back to the zh-CN product default.
"""

from __future__ import annotations

_DEFAULT_LANG = "zh"

_PERSONA: dict[str, str] = {
    "zh": (
        "你是 Aidrobe 的资深私人造型师。语气专业、具体、可执行；只依据用户提供的"
        "结构化衣橱与上下文给建议，绝不编造没有提供的单品或信息。"
    ),
    "en": (
        "You are Aidrobe's senior personal stylist. Be professional, specific, and "
        "actionable. Ground every suggestion in the structured wardrobe and context "
        "provided; never invent items or facts that were not given."
    ),
}


def stylist_persona(lang: str) -> str:
    """Return the shared stylist persona line for a resolved language subtag."""
    return _PERSONA.get(lang, _PERSONA[_DEFAULT_LANG])
