"""Infer an indoor-main climate tendency from temperature-mismatch events plus
commute exposure (BE#82).

Pure, DB-free derivation over `RecommendationEvent`-shaped rows, recomputed each
generation — unit-testable, no persistence.

Safety rules (per the issue):
- A single one-off mismatch must NOT flip inference — a tendency requires
  repeated same-direction reports within the window (`min_mismatches`).
- Commute exposure only *lowers the bar* for an already-present event signal;
  it never fabricates a tendency from zero mismatches.
- Output is a fixed token (`indoor_cold` / `indoor_warm`), never a user-facing
  label like "室内通勤用户".
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from app.common.enums import RecommendationEventType

INDOOR_COLD = "indoor_cold"
INDOOR_WARM = "indoor_warm"

# Default window and thresholds. more_outdoor / unknown users spend more time in
# real weather, so their indoor signal is weaker → require more reports before
# inferring; less_outdoor users are indoor-dominant → a lower bar is trustworthy.
# Never below 2, so a single mismatch can never overfit.
_WINDOW_DAYS = 30
_MIN_MISMATCHES_LESS_OUTDOOR = 2
_MIN_MISMATCHES_DEFAULT = 3


@dataclass(frozen=True)
class IndoorClimateSignal:
    """A derived indoor-climate tendency. `confidence` is always "medium" — this
    is a statistical/heuristic inference, per the reasoning rubric."""

    tendency: str  # INDOOR_COLD | INDOOR_WARM
    confidence: str = "medium"


class _EventLike(Protocol):
    event_type: str
    event_metadata: dict[str, Any] | None
    signal_strength: str | None
    created_at: datetime


def infer_indoor_climate(
    events: Iterable[_EventLike],
    commute_exposure: str | None,
    *,
    now: datetime,
    window_days: int = _WINDOW_DAYS,
) -> IndoorClimateSignal | None:
    """Return a tendency when repeated same-direction temperature mismatches
    cross the confidence threshold, else None.

    A `too_cold` report (user felt cold) means they run cold in their indoor-main
    environment → INDOOR_COLD; `too_warm` → INDOOR_WARM.
    """
    cutoff = now - timedelta(days=window_days)
    cold = 0
    warm = 0
    for event in events:
        if event.event_type != RecommendationEventType.TEMPERATURE_MISMATCH_REPORTED.value:
            continue
        # Confidence-gating: weak signals are implicit/inferred and must not drive
        # a tendency on their own — only medium/strong reports are counted.
        if getattr(event, "signal_strength", None) == "weak":
            continue
        created = event.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=now.tzinfo)
        if created < cutoff:
            continue
        reason = (event.event_metadata or {}).get("reason")
        if reason == "too_cold":
            cold += 1
        elif reason == "too_warm":
            warm += 1

    threshold = (
        _MIN_MISMATCHES_LESS_OUTDOOR
        if commute_exposure == "less_outdoor"
        else _MIN_MISMATCHES_DEFAULT
    )

    # Pick the dominant direction; require it to clear the threshold AND to
    # outweigh the opposite (mixed noise shouldn't infer).
    if cold >= threshold and cold > warm:
        return IndoorClimateSignal(tendency=INDOOR_COLD)
    if warm >= threshold and warm > cold:
        return IndoorClimateSignal(tendency=INDOOR_WARM)
    return None
