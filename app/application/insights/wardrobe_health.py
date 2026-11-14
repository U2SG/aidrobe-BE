"""Wardrobe health scoring for issue #49.

The API is i18n-safe by design: it returns stable machine codes
(``type`` / ``level`` / ``severity``) plus numeric ``metrics``, never
user-facing copy. The frontend localizes from these codes. See the schema
docstrings in ``app/presentation/api/v1/schemas/insights.py`` for the
authoritative contract.
"""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.insights.idle import idle_since_expr
from app.infrastructure.persistence.models.wardrobe import WardrobeItem

# Single coverage weight. ``coverage_ratio`` (share of items worn in the last
# 90 days) is the only recent-wear signal that feeds the score. An earlier
# version also subtracted ``unused_90_ratio * 35``; that is the exact complement
# of ``coverage_ratio`` over the *same* 90-day window, so the two terms
# collapsed to a single ``100 - (1 - coverage_ratio) * 75`` term. We make that
# explicit here. ``unused_counts`` is still computed for the ``unused_items``
# insight metrics, but does NOT feed the score.
_COVERAGE_WEIGHT = 75.0
_COVERAGE_WINDOW_DAYS = 90


# Metric values are numeric scalars, except ``category_balance.counts`` which is
# a per-category count map. The nested dict is the only non-scalar value.
MetricValue = int | float | dict[str, int]


@dataclass(frozen=True)
class WardrobeHealthInsight:
    type: str
    severity: str
    metrics: dict[str, MetricValue]


@dataclass(frozen=True)
class WardrobeHealthResult:
    score: int
    level: str
    insights: list[WardrobeHealthInsight]


@dataclass(frozen=True)
class _ItemSnapshot:
    id: uuid.UUID
    category: str
    last_worn_at: date | None
    idle_since: date


async def build_health(
    user_id: uuid.UUID, session: AsyncSession, as_of: date | None = None
) -> WardrobeHealthResult:
    """Build a deterministic wardrobe health summary from existing item data."""
    today = as_of or date.today()
    items = await _load_items(user_id, session)
    if not items:
        return WardrobeHealthResult(
            score=0,
            level="low",
            insights=[
                WardrobeHealthInsight(
                    type="data_sparsity",
                    severity="low",
                    metrics={"item_count": 0},
                )
            ],
        )

    # `WardrobeItem.last_worn_at` is the authoritative wear signal — it is
    # forward-updated by `WearOutfit.execute` on every recorded wear. Siblings
    # (wardrobe_report / obstacles / answer_question) read it directly; do the
    # same rather than re-deriving last-worn from WearHistory.
    last_worn_values = [item.last_worn_at for item in items]

    total = len(items)
    coverage_ratio = _coverage_ratio(last_worn_values, today, _COVERAGE_WINDOW_DAYS)
    # "Unused" shares the idle definition (last worn, else date added) with the
    # insights summary / idle-items / report; see ``insights.idle``.
    idle_since_values = [item.idle_since for item in items]
    unused_counts = {
        window: _unused_count(idle_since_values, today, window) for window in (60, 90, 180)
    }
    dominant_category, dominant_ratio, category_counts = _category_balance(items)

    score = _score(coverage_ratio, dominant_ratio)
    level = _level(score)
    insights = [
        _coverage_insight(coverage_ratio),
        _unused_insight(unused_counts, total),
        _category_insight(dominant_category, dominant_ratio, category_counts),
    ]

    return WardrobeHealthResult(score=score, level=level, insights=insights)


async def _load_items(user_id: uuid.UUID, session: AsyncSession) -> list[_ItemSnapshot]:
    result = await session.execute(
        select(
            WardrobeItem.id,
            WardrobeItem.category,
            WardrobeItem.last_worn_at,
            idle_since_expr().label("idle_since"),
        )
        .where(
            WardrobeItem.user_id == user_id,
            WardrobeItem.deleted_at.is_(None),
        )
        .order_by(WardrobeItem.id)
    )
    return [
        _ItemSnapshot(
            id=row.id,
            category=row.category,
            last_worn_at=row.last_worn_at,
            idle_since=row.idle_since,
        )
        for row in result.all()
    ]


def _coverage_ratio(last_worn_dates: Iterable[date | None], today: date, days: int) -> float:
    dates = list(last_worn_dates)
    if not dates:
        return 0.0
    cutoff = today - timedelta(days=days)
    worn_count = sum(1 for worn_date in dates if worn_date is not None and worn_date >= cutoff)
    return worn_count / len(dates)


def _unused_count(idle_since_dates: Iterable[date], today: date, days: int) -> int:
    cutoff = today - timedelta(days=days)
    return sum(1 for idle_since in idle_since_dates if idle_since < cutoff)


def _category_balance(items: list[_ItemSnapshot]) -> tuple[str | None, float, dict[str, int]]:
    if not items:
        return None, 0.0, {}
    counts = Counter(item.category for item in items)
    category, count = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0]
    return category, count / len(items), dict(counts)


def _score(coverage_ratio: float, dominant_ratio: float) -> int:
    """Deterministic 0-100 health score.

    Two terms only:
    - recent-wear coverage: ``(1 - coverage_ratio) * _COVERAGE_WEIGHT`` (75).
    - category dominance penalty: linear above a 0.5 share, capped by the 0.5
      span so a fully single-category wardrobe (ratio 1.0) costs 25 points.

    Pinned by ``tests/unit/test_wardrobe_health_score.py``.
    """
    dominance_penalty = max(0.0, dominant_ratio - 0.5) * 50
    raw = 100 - ((1 - coverage_ratio) * _COVERAGE_WEIGHT) - dominance_penalty
    return max(0, min(100, int(round(raw))))


def _level(score: int) -> str:
    if score >= 85:
        return "great"
    if score >= 70:
        return "good"
    if score >= 50:
        return "fair"
    return "low"


def _coverage_insight(coverage_ratio: float) -> WardrobeHealthInsight:
    if coverage_ratio >= 0.75:
        severity = "low"
    elif coverage_ratio >= 0.5:
        severity = "medium"
    else:
        severity = "high"
    return WardrobeHealthInsight(
        type="wear_coverage",
        severity=severity,
        metrics={
            "coverage_ratio": round(coverage_ratio, 4),
            "window_days": _COVERAGE_WINDOW_DAYS,
        },
    )


def _unused_insight(unused_counts: dict[int, int], total: int) -> WardrobeHealthInsight:
    unused_90_ratio = unused_counts[90] / total if total else 0.0
    if unused_90_ratio >= 0.5:
        severity = "high"
    elif unused_90_ratio >= 0.25:
        severity = "medium"
    else:
        severity = "low"
    return WardrobeHealthInsight(
        type="unused_items",
        severity=severity,
        metrics={
            "unused_60": unused_counts[60],
            "unused_90": unused_counts[90],
            "unused_180": unused_counts[180],
        },
    )


def _category_insight(
    dominant_category: str | None, dominant_ratio: float, category_counts: dict[str, int]
) -> WardrobeHealthInsight:
    if dominant_category is None:
        return WardrobeHealthInsight(
            type="category_balance",
            severity="low",
            metrics={},
        )

    if dominant_ratio > 0.65:
        severity = "medium"
    else:
        severity = "low"
    return WardrobeHealthInsight(
        type="category_balance",
        severity=severity,
        metrics={
            "dominant_ratio": round(dominant_ratio, 4),
            "counts": category_counts,
        },
    )
