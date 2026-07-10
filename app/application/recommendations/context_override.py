"""Resolve a Today context override (chip or free text) into a small, fixed set
of generation constraints (BE#78).

Two paths:
- chip_id → a deterministic constraint list (no LLM).
- free text → the LLM maps it onto the SAME fixed taxonomy (json_mode), and the
  result is filtered back to the taxonomy so nothing outside it can take effect.

Safety: the constraint taxonomy is a closed set of tokens — user free text is
passed only as the LLM `question` (never spliced into the system prompt), and the
LLM output is intersected with the taxonomy, so a prompt-injection attempt can at
most select existing constraints, never invent behavior. Constraints are
session-scoped (applied to one generation), never persisted as a preference.
"""

from __future__ import annotations

import json

from app.application.ports.llm import LlmPort

# Closed constraint taxonomy (engine knows how to apply each).
EXCLUDE_DRESS = "exclude_dress"
FORMAL = "formal"
COMFORT = "comfort"
REMOVABLE_LAYER = "removable_layer"
LOW_KEY = "low_key"
REDUCE_WEATHER = "reduce_weather"

CONSTRAINT_TAXONOMY: frozenset[str] = frozenset(
    {EXCLUDE_DRESS, FORMAL, COMFORT, REMOVABLE_LAYER, LOW_KEY, REDUCE_WEATHER}
)

# FE chips (src/screens/today-v2/TodayV2Screen.tsx) → constraints. walk has no
# shoe-walkability metadata to act on, so it degrades to comfort.
CHIP_CONSTRAINTS: dict[str, list[str]] = {
    "meeting": [FORMAL],
    "comfy": [COMFORT],
    "noSkirt": [EXCLUDE_DRESS],
    "walk": [COMFORT],
    "lowKey": [LOW_KEY],
    "coldOffice": [REMOVABLE_LAYER],
    "stayIn": [REDUCE_WEATHER],
}

_LLM_SYSTEM_PROMPT = (
    "You map a user's short Chinese outfit-context note onto a FIXED set of "
    "constraint tokens. Choose ONLY from this exact list and never invent tokens:\n"
    f"{', '.join(sorted(CONSTRAINT_TAXONOMY))}\n"
    "Meanings: exclude_dress=avoid dresses/skirts; formal=more polished/meeting-safe; "
    "comfort=prioritize comfort/mobility; removable_layer=prefer a removable outer "
    "layer (indoor cold); low_key=understated colors; reduce_weather=weather matters "
    "less (mostly indoors).\n"
    'Reply with JSON only: {"constraints": ["token", ...]}. Use [] if none apply.'
)


def _dedupe(tokens: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for token in tokens:
        if token in CONSTRAINT_TAXONOMY and token not in seen:
            seen.add(token)
            out.append(token)
    return out


def constraints_from_chip(chip_id: str | None) -> list[str]:
    if not chip_id:
        return []
    return list(CHIP_CONSTRAINTS.get(chip_id, []))


async def constraints_from_text(text: str | None, llm: LlmPort | None) -> list[str]:
    """Map free text onto the taxonomy via the LLM (json_mode). Degrades to []
    when there is no text, no LLM/key, or the response can't be parsed — an
    override we can't understand simply applies no constraint (honest, not a 500)."""
    if not text or not text.strip() or llm is None:
        return []
    try:
        answer = await llm.ask(_LLM_SYSTEM_PROMPT, text.strip(), json_mode=True)
    except Exception:
        return []
    if not answer.text.strip():
        return []
    try:
        parsed = json.loads(answer.text)
        raw = parsed.get("constraints", []) if isinstance(parsed, dict) else []
    except (json.JSONDecodeError, AttributeError):
        return []
    if not isinstance(raw, list):
        return []
    return _dedupe([t for t in raw if isinstance(t, str)])


async def resolve_constraints(
    chip_id: str | None, text: str | None, source: str | None, llm: LlmPort | None
) -> list[str]:
    """Combine chip + free-text constraints. A chip is authoritative and cheap;
    free text is only LLM-parsed when there's no chip (source == 'free_text' or
    no chip_id), keeping the common chip path off the LLM entirely."""
    chip = constraints_from_chip(chip_id)
    if chip:
        return chip
    if source == "chip":
        return []
    return await constraints_from_text(text, llm)
