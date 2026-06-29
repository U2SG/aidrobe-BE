"""Render durable preference memory as a PRIORITIZED prompt section (BE#66).

The pre-#66 prompt folded preferences into a JSON context block where they were
"included but not prioritized". This renders them as explicit rules: negatives
become hard avoid-rules (listed first), positives become soft preferences — so
the model treats them with the right weight.

Input is the compact shape from
`app.application.preferences.memory.read_preference_memory`:
`{"positive": [{type, value, confidence}], "negative": [...]}`. Values are
already bounded at write time; we defensively re-bound via
`sanitize_preference_value` so no unbounded user text reaches the prompt.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from app.infrastructure.llm.sanitizer import sanitize_preference_value

_DEFAULT_LANG = "zh"

# (hard-avoid, soft-avoid, prefer) headers per language. BE#69 review: a negative
# row derived from feedback/inference is intentionally capped at low confidence so
# one behavioral signal can't become a strong preference — so it must NOT be
# rendered with the same "MUST AVOID, highest priority" weight as an explicit
# dislike. Strong negatives → hard avoid; weak ones → a softer lean-away hint.
_HEADERS: dict[str, tuple[str, str, str]] = {
    "zh": (
        "必须避免（用户明确不喜欢，最高优先级）：",
        "尽量避免（较弱倾向，可权衡）：",
        "倾向偏好（在可行时优先满足）：",
    ),
    "en": (
        "MUST AVOID (the user explicitly dislikes these — highest priority):",
        "LEAN AWAY (weaker signal — weigh against other factors):",
        "PREFER (favor these when feasible):",
    ),
}
# Confidence at/above which a negative is a hard rule. Explicit statements are
# written at ~0.9; feedback/inference are capped at 0.4 — so 0.5 cleanly splits
# them. Entries with no confidence (legacy/explicit callers) default to strong.
_STRONG_CONFIDENCE = 0.5
# Bound how many entries per polarity reach the prompt (read side already caps,
# this is a second floor so a future caller can't blow the prompt up).
_MAX_PER_POLARITY = 8


def _render_entries(entries: Any) -> list[tuple[str, float]]:
    """Return (rendered text, confidence) for each valid entry. Missing
    confidence defaults to 1.0 so explicit/legacy callers stay "strong"."""
    out: list[tuple[str, float]] = []
    if not isinstance(entries, list):
        return out
    for entry in entries[:_MAX_PER_POLARITY]:
        if not isinstance(entry, Mapping):
            continue
        value = sanitize_preference_value(str(entry.get("value", "")))
        if not value:
            continue
        ptype = sanitize_preference_value(str(entry.get("type", "")))
        raw_conf = entry.get("confidence", 1.0)
        confidence = float(raw_conf) if isinstance(raw_conf, int | float) else 1.0
        out.append((f"{ptype}: {value}" if ptype else value, confidence))
    return out


def render_preferences(
    memory: Mapping[str, Any] | None, lang: str
) -> str | None:
    """Render preference memory as prioritized avoid/prefer rules.

    Returns None when there is nothing to say, so the caller drops the section
    entirely (keeping a no-preference prompt clean).
    """
    if not memory:
        return None
    negatives = _render_entries(memory.get("negative"))
    positives = _render_entries(memory.get("positive"))
    if not negatives and not positives:
        return None
    strong = [text for text, conf in negatives if conf >= _STRONG_CONFIDENCE]
    weak = [text for text, conf in negatives if conf < _STRONG_CONFIDENCE]
    hard_header, soft_header, prefer_header = _HEADERS.get(lang, _HEADERS[_DEFAULT_LANG])
    lines: list[str] = []
    if strong:
        lines.append(hard_header)
        lines.extend(f"- {n}" for n in strong)
    if weak:
        lines.append(soft_header)
        lines.extend(f"- {n}" for n in weak)
    if positives:
        lines.append(prefer_header)
        lines.extend(f"- {p}" for p, _ in positives)
    return "\n".join(lines)
