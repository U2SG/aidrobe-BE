"""Convert recommendation feedback events into structured learning signals (BE#67).

Pure, DB-free derivation over a list of `RecommendationEvent`-shaped objects, so
it is unit-testable and the same function serves both the ranking path (suppress
recently dismissed/disliked outfit item-sets) and the preference-memory path
(controlled WEAK updates from REPEATED feedback).

Safety rules baked in (per the issue):
- One-off feedback never becomes a durable preference — a signal must recur at
  least `durable_min_count` times in the window.
- Derived signals are fixed taxonomy TOKENS (never raw user text), so nothing
  here can smuggle user input into a prompt.
- Suppression is derived at read time (recomputed each generation) and is a
  bounded penalty, not a hard exclusion — it stays reversible.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any, Protocol

from app.common.enums import RecommendationEventType

# Canonical feedback taxonomy (issue §Feedback Taxonomy). Reason-tag keys + event
# types normalize INTO these tokens; unknown inputs are ignored.
SIGNAL_DISLIKED = "disliked"
SIGNAL_DISMISSED = "dismissed"
SIGNAL_TOO_FORMAL = "too_formal"
SIGNAL_TOO_CASUAL = "too_casual"
SIGNAL_WRONG_FIT = "wrong_fit"
SIGNAL_NOT_MY_STYLE = "not_my_style"

# Event types that mean "do not show this look again" → drive suppression.
_SUPPRESSION_EVENT_TYPES = {
    RecommendationEventType.DISLIKED.value,
    RecommendationEventType.DISMISSED.value,
}
# Positive signals on the SAME look that revoke a suppression (the user changed
# their mind). A signature is suppressed only if its most recent dislike/dismiss
# is newer than its most recent positive event.
# Only LIKED qualifies today: revocation matches on item-set signature, and the
# ONLY emitter that stores `event_metadata["item_ids"]` is `FeedbackOutfit`
# (love/like → LIKED). SAVED/WORN events (save/wear endpoints) carry no signature,
# so they can't match a suppressed look — including them would be dead code.
_POSITIVE_EVENT_TYPES = {
    RecommendationEventType.LIKED.value,
}

# Reason-tag key → canonical signal. Only the self-describing reasons that map to
# a durable preference are listed; contextual reasons (too_warm/too_cold/itchy)
# are deliberately NOT here (they describe the day, not a durable preference).
_REASON_TAG_TO_SIGNAL: dict[str, str] = {
    "too_formal": SIGNAL_TOO_FORMAL,
    "too_casual": SIGNAL_TOO_CASUAL,
    "wrong_fit": SIGNAL_WRONG_FIT,
    "not_my_style": SIGNAL_NOT_MY_STYLE,
}

# Repeated durable signal → (preference_type, preference_value, polarity). Values
# are canonical tokens (never raw user text), so they are prompt-safe when #66's
# render_preferences surfaces them.
_SIGNAL_TO_PREFERENCE: dict[str, tuple[str, str, str]] = {
    SIGNAL_TOO_FORMAL: ("formality", "too_formal", "negative"),
    SIGNAL_TOO_CASUAL: ("formality", "too_casual", "negative"),
    SIGNAL_WRONG_FIT: ("fit", "wrong_fit", "negative"),
    SIGNAL_NOT_MY_STYLE: ("style", "not_my_style", "negative"),
}

DEFAULT_SUPPRESSION_DAYS = 14
DEFAULT_DURABLE_MIN_COUNT = 3


class FeedbackEvent(Protocol):
    """The subset of RecommendationEvent this module reads.

    `event_metadata` is typed `Any` (not `Mapping`) so a mutable ORM `dict`
    column satisfies the protocol — mypy matches data members invariantly. The
    helpers below guard it with `isinstance(..., Mapping)`."""

    event_type: str
    event_metadata: Any
    created_at: datetime


@dataclass(frozen=True)
class WeakPreference:
    preference_type: str
    preference_value: str
    polarity: str


@dataclass(frozen=True)
class LearningSignals:
    # Item-sets the user recently dismissed/disliked → down-rank look-alikes.
    suppressed_signatures: list[frozenset[str]] = field(default_factory=list)
    # Durable preferences derived from REPEATED feedback (one-offs excluded).
    weak_preferences: list[WeakPreference] = field(default_factory=list)


def normalize_event(event_type: str, reason_tags: Iterable[str] | None) -> set[str]:
    """Normalize one event's type + reason tags into canonical signal tokens."""
    signals: set[str] = set()
    if event_type in _SUPPRESSION_EVENT_TYPES:
        signals.add(event_type)  # "disliked" / "dismissed"
    for tag in reason_tags or []:
        mapped = _REASON_TAG_TO_SIGNAL.get(str(tag).strip().lower())
        if mapped is not None:
            signals.add(mapped)
    return signals


def _signature(metadata: Mapping[str, Any] | None) -> frozenset[str] | None:
    if not isinstance(metadata, Mapping):
        return None
    raw = metadata.get("item_ids")
    if not isinstance(raw, list) or not raw:
        return None
    return frozenset(str(i) for i in raw)


def _reason_tags(metadata: Mapping[str, Any] | None) -> list[str]:
    if not isinstance(metadata, Mapping):
        return []
    raw = metadata.get("reason_tags")
    return [str(t) for t in raw] if isinstance(raw, list) else []


def derive_learning_signals(
    events: Iterable[FeedbackEvent],
    *,
    now: datetime,
    suppression_days: int = DEFAULT_SUPPRESSION_DAYS,
    durable_min_count: int = DEFAULT_DURABLE_MIN_COUNT,
) -> LearningSignals:
    """Aggregate recent feedback events into bounded learning signals.

    `events` need not be pre-filtered; only those within `suppression_days`
    contribute. Suppression collects item-set signatures of dismissed/disliked
    outfits; weak preferences require a signal to recur `>= durable_min_count`
    times so a single interaction never becomes a durable preference.
    """
    cutoff = now - timedelta(days=suppression_days)
    signal_counts: Counter[str] = Counter()
    # Per signature, the most-recent suppression vs positive timestamps. A later
    # like/save/wear on the same look revokes an earlier dislike/dismiss.
    suppress_at: dict[frozenset[str], datetime] = {}
    positive_at: dict[frozenset[str], datetime] = {}
    # Preserve first-seen order of suppressed signatures for a stable result.
    order: list[frozenset[str]] = []

    for event in events:
        created = event.created_at
        if created is not None and created < cutoff:
            continue
        stamp = created if created is not None else now
        signature = _signature(event.event_metadata)
        if signature is not None:
            if event.event_type in _SUPPRESSION_EVENT_TYPES:
                if signature not in suppress_at:
                    order.append(signature)
                suppress_at[signature] = max(suppress_at.get(signature, stamp), stamp)
            elif event.event_type in _POSITIVE_EVENT_TYPES:
                positive_at[signature] = max(positive_at.get(signature, stamp), stamp)
        # Durable-preference counting (only the self-describing reason signals).
        signals = normalize_event(event.event_type, _reason_tags(event.event_metadata))
        for signal in signals:
            if signal in _SIGNAL_TO_PREFERENCE:
                signal_counts[signal] += 1

    suppressed = [
        sig
        for sig in order
        if sig not in positive_at or suppress_at[sig] > positive_at[sig]
    ]

    weak: list[WeakPreference] = []
    for signal, count in signal_counts.items():
        if count >= durable_min_count:
            ptype, pvalue, polarity = _SIGNAL_TO_PREFERENCE[signal]
            weak.append(WeakPreference(ptype, pvalue, polarity))

    return LearningSignals(suppressed_signatures=suppressed, weak_preferences=weak)
