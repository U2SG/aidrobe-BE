"""Wardrobe health scoring for issue #49."""

from __future__ import annotations

import uuid
from collections import Counter
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date, timedelta
from typing import cast

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.persistence.models.recommendation import OutfitItem, WearHistory
from app.infrastructure.persistence.models.wardrobe import WardrobeItem


@dataclass(frozen=True)
class WardrobeHealthInsight:
    type: str
    severity: str
    title: str
    action: str


@dataclass(frozen=True)
class WardrobeHealthResult:
    score: int
    level: str
    summary: str
    insights: list[WardrobeHealthInsight]


@dataclass(frozen=True)
class _ItemSnapshot:
    id: uuid.UUID
    category: str
    last_worn_at: date | None


async def build_health(
    user_id: uuid.UUID, session: AsyncSession, as_of: date | None = None
) -> WardrobeHealthResult:
    """Build a deterministic wardrobe health summary from existing item/wear data."""
    today = as_of or date.today()
    items = await _load_items(user_id, session)
    if not items:
        return WardrobeHealthResult(
            score=0,
            level="low",
            summary="Add a few wardrobe items to unlock a health score.",
            insights=[
                WardrobeHealthInsight(
                    type="data_sparsity",
                    severity="low",
                    title="No wardrobe items are available yet",
                    action="Start with a few everyday pieces, then check back for richer insights.",
                )
            ],
        )

    history_last_worn = await _load_history_last_worn(
        user_id, session, [item.id for item in items]
    )
    last_worn_by_item = {
        item.id: _latest_date(item.last_worn_at, history_last_worn.get(item.id))
        for item in items
    }

    total = len(items)
    coverage_ratio = _coverage_ratio(last_worn_by_item.values(), today, 90)
    unused_counts = {
        window: _unused_count(last_worn_by_item.values(), today, window)
        for window in (60, 90, 180)
    }
    dominant_category, dominant_ratio = _category_balance(items)

    score = _score(coverage_ratio, unused_counts[90] / total, dominant_ratio)
    level = _level(score)
    insights = [
        _coverage_insight(coverage_ratio),
        _unused_insight(unused_counts, total),
        _category_insight(dominant_category, dominant_ratio),
    ]

    return WardrobeHealthResult(
        score=score,
        level=level,
        summary=_summary(level),
        insights=insights,
    )


async def _load_items(user_id: uuid.UUID, session: AsyncSession) -> list[_ItemSnapshot]:
    result = await session.execute(
        select(WardrobeItem.id, WardrobeItem.category, WardrobeItem.last_worn_at)
        .where(
            WardrobeItem.user_id == user_id,
            WardrobeItem.deleted_at.is_(None),
        )
        .order_by(WardrobeItem.id)
    )
    return [
        _ItemSnapshot(id=row.id, category=row.category, last_worn_at=row.last_worn_at)
        for row in result.all()
    ]


async def _load_history_last_worn(
    user_id: uuid.UUID, session: AsyncSession, item_ids: list[uuid.UUID]
) -> dict[uuid.UUID, date]:
    if not item_ids:
        return {}

    result = await session.execute(
        select(OutfitItem.item_id, func.max(WearHistory.worn_date).label("last_worn"))
        .select_from(WearHistory)
        .join(OutfitItem, OutfitItem.outfit_id == WearHistory.outfit_id)
        .where(
            WearHistory.user_id == user_id,
            OutfitItem.item_id.in_(item_ids),
        )
        .group_by(OutfitItem.item_id)
    )
    last_worn_by_item: dict[uuid.UUID, date] = {}
    for row in result.all():
        if row.last_worn is not None:
            last_worn_by_item[cast(uuid.UUID, row.item_id)] = cast(date, row.last_worn)
    return last_worn_by_item


def _latest_date(a: date | None, b: date | None) -> date | None:
    if a is None:
        return b
    if b is None:
        return a
    return max(a, b)


def _coverage_ratio(last_worn_dates: Iterable[date | None], today: date, days: int) -> float:
    dates = list(last_worn_dates)
    if not dates:
        return 0.0
    cutoff = today - timedelta(days=days)
    worn_count = sum(1 for worn_date in dates if worn_date is not None and worn_date >= cutoff)
    return worn_count / len(dates)


def _unused_count(last_worn_dates: Iterable[date | None], today: date, days: int) -> int:
    cutoff = today - timedelta(days=days)
    return sum(1 for worn_date in last_worn_dates if worn_date is None or worn_date < cutoff)


def _category_balance(items: list[_ItemSnapshot]) -> tuple[str | None, float]:
    if not items:
        return None, 0.0
    counts = Counter(item.category for item in items)
    category, count = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0]
    return category, count / len(items)


def _score(coverage_ratio: float, unused_90_ratio: float, dominant_ratio: float) -> int:
    dominance_penalty = max(0.0, dominant_ratio - 0.5) * 50
    raw = 100 - ((1 - coverage_ratio) * 40) - (unused_90_ratio * 35) - dominance_penalty
    return max(0, min(100, int(round(raw))))


def _level(score: int) -> str:
    if score >= 85:
        return "great"
    if score >= 70:
        return "good"
    if score >= 50:
        return "fair"
    return "low"


def _summary(level: str) -> str:
    summaries = {
        "great": "Your wardrobe has strong recent coverage and a balanced mix.",
        "good": "Your wardrobe is in a good place, with a few items worth revisiting.",
        "fair": "Your wardrobe has useful pieces, and a few areas could use attention.",
        "low": "There is limited recent wear data, so this is a gentle starting point.",
    }
    return summaries[level]


def _coverage_insight(coverage_ratio: float) -> WardrobeHealthInsight:
    percent = int(round(coverage_ratio * 100))
    if coverage_ratio >= 0.75:
        severity = "low"
        action = "Keep rotating pieces you already enjoy wearing."
    elif coverage_ratio >= 0.5:
        severity = "medium"
        action = "Try pairing one less-used item with a favorite outfit this week."
    else:
        severity = "high"
        action = "Pick one easy-to-style item you have not worn recently and build around it."
    return WardrobeHealthInsight(
        type="wear_coverage",
        severity=severity,
        title=f"{percent}% of your wardrobe was worn in the last 90 days",
        action=action,
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
        title=(
            f"Unused items: {unused_counts[60]} in 60d, "
            f"{unused_counts[90]} in 90d, {unused_counts[180]} in 180d"
        ),
        action="Revisit one unused piece at a time; keep what still feels useful.",
    )


def _category_insight(
    dominant_category: str | None, dominant_ratio: float
) -> WardrobeHealthInsight:
    if dominant_category is None:
        return WardrobeHealthInsight(
            type="category_balance",
            severity="low",
            title="Category balance is not available yet",
            action="Add more wardrobe items to make this insight more useful.",
        )

    category = dominant_category.replace("_", " ").title()
    percent = int(round(dominant_ratio * 100))
    if dominant_ratio > 0.65:
        severity = "medium"
        action = "When planning future outfits, consider the categories that unlock more combinations."
    elif dominant_ratio > 0.5:
        severity = "low"
        action = "Your wardrobe can still work well; a few complementary pieces may add flexibility."
    else:
        severity = "low"
        action = "Keep using this balanced mix as a base for new outfit ideas."
    title = (
        f"{category} makes up {percent}% of your wardrobe"
        if dominant_ratio > 0.5
        else "Your categories are balanced"
    )
    return WardrobeHealthInsight(
        type="category_balance",
        severity=severity,
        title=title,
        action=action,
    )
