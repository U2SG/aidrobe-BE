"""BE#46 — persistent user preference memory service.

Two responsibilities:

* **read** — `read_preference_memory` returns the compact, prompt-safe
  `{"positive": [...], "negative": [...]}` shape the issue specifies, capped so
  the prompt block stays bounded.
* **controlled write** — `record_preference` persists ONE explicit preference.
  Writes only from trusted sources; a single behavioral (`feedback_signal`)
  observation is capped so it can never become a strong preference on its own
  ("do not over-infer from one-off behavior"). Merge/update is delegated to the
  repo's partial-unique upsert, which also **prefers explicit statements**: a
  weak (feedback / inferred) source never overwrites a row an explicit user
  statement set; explicit always wins; weak-over-weak is last-writer-wins.

Persistence is unconditional (no feature flag here): the flag only gates whether
Ask / Today *read* this memory. That keeps a flag flip a clean rollback.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.infrastructure.llm.sanitizer import sanitize_preference_value
from app.infrastructure.persistence.models.preference_memory import UserPreferenceMemory
from app.infrastructure.persistence.repositories.preference_memory_repo import (
    SqlAlchemyPreferenceMemoryRepository,
)

# Trusted write sources. `inferred_from_history` is allowed but, like
# feedback_signal, capped below — neither may assert a strong preference alone.
SOURCE_EXPLICIT = "explicit_user_statement"
SOURCE_FEEDBACK = "feedback_signal"
SOURCE_INFERRED = "inferred_from_history"
_TRUSTED_SOURCES = frozenset({SOURCE_EXPLICIT, SOURCE_FEEDBACK, SOURCE_INFERRED})

# Single non-explicit observations cannot exceed this confidence — a skipped
# outfit is weak evidence, not a strong negative preference.
_WEAK_SOURCE_CONFIDENCE_CAP = 0.4

_VALID_POLARITIES = frozenset({"positive", "negative", "neutral"})

# Bound the prompt-facing memory so the injected block stays compact.
_MAX_PER_POLARITY = 8


class PreferenceMemoryError(ValueError):
    """Rejected write (bad polarity / untrusted source / empty value)."""


def _clamp_confidence(value: float, source: str) -> float:
    confidence = min(1.0, max(0.0, float(value)))
    if source != SOURCE_EXPLICIT:
        # Do not over-infer: a single behavioral/inferred signal is capped so it
        # can't immediately become a strong (negative or positive) preference.
        confidence = min(confidence, _WEAK_SOURCE_CONFIDENCE_CAP)
    return round(confidence, 2)


async def record_preference(
    repo: SqlAlchemyPreferenceMemoryRepository,
    *,
    user_id: uuid.UUID,
    preference_type: str,
    preference_value: str,
    polarity: str,
    confidence: float,
    source: str,
    source_message_id: uuid.UUID | None = None,
) -> UserPreferenceMemory:
    """Controlled write of a single explicit preference. Raises
    PreferenceMemoryError on an invalid request; re-stating an existing
    (user, type, value) updates it in place (repo upsert)."""
    if source not in _TRUSTED_SOURCES:
        raise PreferenceMemoryError(f"untrusted preference source: {source!r}")
    if polarity not in _VALID_POLARITIES:
        raise PreferenceMemoryError(f"invalid polarity: {polarity!r}")
    value = sanitize_preference_value(preference_value)
    if not value:
        raise PreferenceMemoryError("empty preference value after sanitization")
    pref_type = sanitize_preference_value(preference_type)
    if not pref_type:
        raise PreferenceMemoryError("empty preference type")
    return await repo.upsert(
        user_id=user_id,
        preference_type=pref_type,
        preference_value=value,
        polarity=polarity,
        confidence=_clamp_confidence(confidence, source),
        source=source,
        source_message_id=source_message_id,
    )


async def read_preference_memory(
    repo: SqlAlchemyPreferenceMemoryRepository, user_id: uuid.UUID
) -> dict[str, list[dict[str, Any]]]:
    """Compact `{"positive": [...], "negative": [...]}` view for prompts.

    Neutral-polarity rows are omitted — they carry no directional signal for a
    recommender. Each entry is `{type, value, confidence}`; values are already
    bounded at write time. Capped to `_MAX_PER_POLARITY` (rows arrive
    confidence-desc from the repo) so the injected block stays small."""
    rows = await repo.list_live(user_id)
    positive: list[dict[str, Any]] = []
    negative: list[dict[str, Any]] = []
    for row in rows:
        entry = {
            "type": row.preference_type,
            "value": row.preference_value,
            "confidence": float(row.confidence),
        }
        if row.polarity == "positive" and len(positive) < _MAX_PER_POLARITY:
            positive.append(entry)
        elif row.polarity == "negative" and len(negative) < _MAX_PER_POLARITY:
            negative.append(entry)
    return {"positive": positive, "negative": negative}


def negative_values_by_type(
    memory: dict[str, list[dict[str, Any]]], preference_type: str
) -> set[str]:
    """Lowercased negative `value`s of a given `preference_type` — the engine's
    hook for letting an explicit negative (e.g. a disliked color) down-rank
    matching candidates. Empty when there is no such negative."""
    return {
        str(e["value"]).lower()
        for e in memory.get("negative", [])
        if e.get("type") == preference_type and isinstance(e.get("value"), str)
    }
