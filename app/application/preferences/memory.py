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
from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from typing import Any

from app.common.enums import FitScene
from app.infrastructure.llm.sanitizer import sanitize_preference_value
from app.infrastructure.persistence.models.preference_memory import UserPreferenceMemory
from app.infrastructure.persistence.repositories.preference_memory_repo import (
    PreferenceEvidenceRevoked,
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

# Allowlist for `preference_type`. This is durable memory that later enters LLM
# prompts, so the type label is pinned to a known vocabulary at write time;
# free-form user text is confined to the bounded `preference_value`. Mirrors the
# type list documented on the model. Types are stored lowercased so the scoring
# read can match them case-insensitively.
_VALID_PREFERENCE_TYPES = frozenset(
    {
        "style",
        "color",
        "fit",
        "occasion",
        "brand",
        "material",
        "formality",
        "climate",
        "footwear",
        "negative_preference",
    }
)

# Bound the prompt-facing memory so the injected block stays compact.
_MAX_PER_POLARITY = 8


class PreferenceMemoryError(ValueError):
    """Rejected write (bad polarity / untrusted source / empty value)."""


def normalize_scope(scope: Mapping[str, str] | None) -> dict[str, str]:
    """Only supported context dimensions can qualify a durable preference."""
    if scope is None:
        return {}
    if not isinstance(scope, Mapping) or set(scope) - {"scene", "day_type"}:
        raise PreferenceMemoryError("unsupported preference scope")
    result: dict[str, str] = {}
    for key, value in scope.items():
        if not isinstance(value, str):
            raise PreferenceMemoryError("invalid preference scope")
        clean = value.strip().lower()
        allowed = {s.value for s in FitScene} if key == "scene" else {"weekday", "weekend"}
        if clean not in allowed:
            raise PreferenceMemoryError("invalid preference scope")
        result[key] = clean
    return result


def scope_text(scope: Mapping[str, str] | None, locale: str) -> str:
    scope = scope or {}
    zh = locale.startswith("zh")
    labels = {
        "commute": "通勤" if zh else "Commute",
        "formal": "正式场合" if zh else "Formal occasions",
        "casual": "休闲" if zh else "Casual occasions",
        "date": "约会" if zh else "Dates",
        "travel": "旅行" if zh else "Travel",
        "weekday": "工作日" if zh else "Weekdays",
        "weekend": "周末" if zh else "Weekends",
    }
    parts: list[str] = []
    for key in ("scene", "day_type"):
        value = scope.get(key)
        if isinstance(value, str) and value in labels:
            parts.append(labels[value])
    return " · ".join(parts) if parts else ("所有场合" if zh else "All occasions")


def resolve_effective_preferences(
    rows: Sequence[UserPreferenceMemory],
    *,
    scene: str | None = None,
    target_date: date | None = None,
    overrides: Sequence[Mapping[str, str]] = (),
    now: datetime | None = None,
) -> list[UserPreferenceMemory]:
    """Resolve context before prompt capping or ranking; never mutate memory.

    A scoped explicit neutral masks the broader dislike without claiming the
    user likes it. Temporary exceptions name one known preference, not a whole
    type, so exploring colour cannot silently relax footwear or comfort rules.
    """
    now = now or datetime.now(UTC)
    day_type = None if target_date is None else ("weekend" if target_date.weekday() >= 5 else "weekday")
    selected: dict[tuple[str, str], UserPreferenceMemory] = {}

    def priority(row: UserPreferenceMemory) -> tuple[bool, int, datetime]:
        return (
            row.source == SOURCE_EXPLICIT,
            len(row.scope or {}),
            row.updated_at or datetime.min.replace(tzinfo=UTC),
        )

    for row in rows:
        if row.deleted_at is not None or (row.expires_at is not None and row.expires_at <= now):
            continue
        scope = row.scope or {}
        if scope.get("scene") and scope["scene"] != (scene or "").lower():
            continue
        if scope.get("day_type") and scope["day_type"] != day_type:
            continue
        key = (row.preference_type.lower(), row.preference_value.casefold())
        previous = selected.get(key)
        if previous is None or priority(row) > priority(previous):
            selected[key] = row

    excepted: set[tuple[str, str]] = set()
    for override in list(overrides)[:8]:
        if not isinstance(override, Mapping):
            continue
        ident = override.get("preference_id")
        ptype, value = override.get("type"), override.get("value")
        for key, row in selected.items():
            if (ident and ident == str(row.id)) or (
                isinstance(ptype, str) and isinstance(value, str)
                and key == (ptype.strip().lower(), value.strip().casefold())
            ):
                excepted.add(key)
    return [row for key, row in selected.items() if key not in excepted]


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
    scope: Mapping[str, str] | None = None,
    expires_at: datetime | None = None,
    evidence_at: datetime | None = None,
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
    pref_type = sanitize_preference_value(preference_type).lower()
    if not pref_type:
        raise PreferenceMemoryError("empty preference type")
    if pref_type not in _VALID_PREFERENCE_TYPES:
        raise PreferenceMemoryError(f"unknown preference type: {pref_type!r}")
    normalized_scope = normalize_scope(scope)
    now = datetime.now(UTC)
    for stamp in (expires_at, evidence_at):
        if stamp is not None and (stamp.tzinfo is None or stamp.utcoffset() is None):
            raise PreferenceMemoryError("preference dates require a timezone")
    if evidence_at is not None and evidence_at > now:
        raise PreferenceMemoryError("preference evidence cannot be in the future")
    if source != SOURCE_EXPLICIT:
        # Decay follows the observations, not the date an old batch is replayed.
        weak_expiry = (evidence_at or now) + timedelta(days=14)
        expires_at = min(expires_at, weak_expiry) if expires_at is not None else weak_expiry
    try:
        return await repo.upsert(
            user_id=user_id,
            preference_type=pref_type,
            preference_value=value,
            polarity=polarity,
            confidence=_clamp_confidence(confidence, source),
            source=source,
            source_message_id=source_message_id,
            scope=normalized_scope,
            expires_at=expires_at,
            evidence_at=evidence_at,
        )
    except PreferenceEvidenceRevoked as exc:
        raise PreferenceMemoryError(str(exc)) from exc


async def read_preference_memory(
    repo: SqlAlchemyPreferenceMemoryRepository, user_id: uuid.UUID,
    *,
    scene: str | None = None,
    target_date: date | None = None,
    overrides: Sequence[Mapping[str, str]] = (),
    now: datetime | None = None,
) -> dict[str, list[dict[str, Any]]]:
    """Compact `{"positive": [...], "negative": [...]}` view for prompts.

    Neutral-polarity rows are omitted — they carry no directional signal for a
    recommender. Each entry is `{type, value, confidence}`; values are already
    bounded at write time. Capped to `_MAX_PER_POLARITY` (rows arrive
    confidence-desc from the repo) so the injected block stays small."""
    rows = resolve_effective_preferences(
        await repo.list_live(user_id), scene=scene, target_date=target_date, overrides=overrides, now=now
    )
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


async def live_negative_values_by_type(
    repo: SqlAlchemyPreferenceMemoryRepository,
    user_id: uuid.UUID,
    *preference_types: str,
    scene: str | None = None,
    target_date: date | None = None,
    overrides: Sequence[Mapping[str, str]] = (),
    now: datetime | None = None,
) -> set[str]:
    """Uncapped, scoring-specific read of negative `value`s for the given types.

    `read_preference_memory` caps each polarity to `_MAX_PER_POLARITY` for prompt
    budget, but RANKING must honor every explicit negative — an explicit
    "dislike red" that ranks behind eight higher-confidence fit/style negatives
    must still down-rank red outfits. Scoring therefore reads the live rows
    directly rather than the capped prompt view. Types are matched against the
    lowercased values persisted by the write path."""
    wanted = {t.lower() for t in preference_types}
    rows = resolve_effective_preferences(
        await repo.list_live(user_id), scene=scene, target_date=target_date, overrides=overrides, now=now
    )
    return {
        row.preference_value.lower()
        for row in rows
        if row.polarity == "negative" and row.preference_type in wanted
    }
