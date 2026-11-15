"""Half-year / annual wardrobe report aggregation — M21.

A Spotify-Wrapped-style report computed from real data for a named period
(e.g. ``2026-H1`` / ``2026-H2`` / ``2026-annual``). All period-scoped metrics
(total wears, top items, combos, palette, style keywords, style trend, adoption)
are computed from ``WearHistory`` joined to the items actually worn in the window
— NOT from the lifetime ``WardrobeItem.wear_count`` counter. Two metrics are
deliberately lifetime / point-in-time and documented as such: cost-per-wear
(purchase_price / lifetime wears — the real "earning its keep" signal) and
longest-idle (point-in-time as of period end via ``last_worn_at``).

Aggregation pushes all filtering/grouping into SQL; nothing pulls the full row
set into Python to filter there.
"""

import asyncio
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from sqlalchemy import Integer, cast, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.insights.idle import idle_since_expr
from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.infrastructure.persistence.models.recommendation import (
    DailyRecommendation,
    Outfit,
    OutfitItem,
    WearHistory,
)
from app.infrastructure.persistence.models.wardrobe import WardrobeItem


@dataclass(frozen=True)
class PeriodWindow:
    """Resolved calendar window for a report period plus its predecessor."""

    period: str
    label: str
    start: date
    end: date  # inclusive
    prev_period: str
    prev_start: date
    prev_end: date  # inclusive


def parse_period(period: str) -> PeriodWindow:
    """Parse ``YYYY-H1`` / ``YYYY-H2`` / ``YYYY-annual`` into a window.

    Malformed input raises ``FriendlyException`` (VALIDATION_ERROR, 422). The
    previous-period mapping is H1→prior-year H2, H2→same-year H1,
    annual→prior-year annual, so the style-trend delta always has a comparable
    predecessor.
    """
    raw = (period or "").strip()
    parts = raw.split("-", 1)
    if len(parts) != 2 or not parts[0].isdigit() or len(parts[0]) != 4:
        raise _invalid(period)
    year = int(parts[0])
    suffix = parts[1].lower()

    if suffix == "h1":
        start, end = date(year, 1, 1), date(year, 6, 30)
        prev_period = f"{year - 1}-H2"
        prev_start, prev_end = date(year - 1, 7, 1), date(year - 1, 12, 31)
        label = f"{year} 上半年"
    elif suffix == "h2":
        start, end = date(year, 7, 1), date(year, 12, 31)
        prev_period = f"{year}-H1"
        prev_start, prev_end = date(year, 1, 1), date(year, 6, 30)
        label = f"{year} 下半年"
    elif suffix == "annual":
        start, end = date(year, 1, 1), date(year, 12, 31)
        prev_period = f"{year - 1}-annual"
        prev_start, prev_end = date(year - 1, 1, 1), date(year - 1, 12, 31)
        label = f"{year} 年度"
    else:
        raise _invalid(period)

    return PeriodWindow(
        period=raw,
        label=label,
        start=start,
        end=end,
        prev_period=prev_period,
        prev_start=prev_start,
        prev_end=prev_end,
    )


def _invalid(period: str) -> FriendlyException:
    return FriendlyException(
        error_codes.VALIDATION_ERROR,
        f"Invalid report period '{period}'. Expected 'YYYY-H1', 'YYYY-H2', or 'YYYY-annual'.",
        status_code=422,
    )


@dataclass
class TopItem:
    item_id: uuid.UUID
    title: str
    category: str
    wear_count: int


@dataclass
class OutfitCombo:
    outfit_id: uuid.UUID
    title: str
    wear_count: int


@dataclass
class CostPerWearEntry:
    item_id: uuid.UUID
    title: str
    purchase_price: Decimal
    wear_count: int
    cost_per_wear: Decimal


@dataclass
class IdleEntry:
    item_id: uuid.UUID
    title: str
    category: str
    last_worn: date | None
    idle_days: int | None


@dataclass
class StyleTrend:
    period: str
    prev_period: str
    current: dict[str, int]
    previous: dict[str, int]
    rising: list[str]
    fading: list[str]


@dataclass
class WardrobeReport:
    period: str
    label: str
    start_date: date
    end_date: date
    is_empty: bool
    total_wears: int
    prev_total_wears: int = 0
    top_items: list[TopItem] = field(default_factory=list)
    top_combos: list[OutfitCombo] = field(default_factory=list)
    color_palette: dict[str, int] = field(default_factory=dict)
    style_keywords: list[str] = field(default_factory=list)
    style_trend: StyleTrend | None = None
    highest_cost_per_wear: CostPerWearEntry | None = None
    longest_idle_item: IdleEntry | None = None
    recommendation_adoption_rate: float = 0.0


async def _count_wears(
    session: AsyncSession, user_id: uuid.UUID, start: date, end: date
) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(WearHistory)
        .where(
            WearHistory.user_id == user_id,
            WearHistory.worn_date >= start,
            WearHistory.worn_date <= end,
        )
    )
    return int(result.scalar_one() or 0)


async def _top_items(
    session: AsyncSession, user_id: uuid.UUID, w: PeriodWindow, limit: int = 3
) -> list[TopItem]:
    """Top-N items by in-period wear count (WearHistory → OutfitItem → item)."""
    wear_count = func.count().label("wear_count")
    result = await session.execute(
        select(
            WardrobeItem.id,
            WardrobeItem.title,
            WardrobeItem.category,
            wear_count,
        )
        .select_from(WearHistory)
        .join(OutfitItem, OutfitItem.outfit_id == WearHistory.outfit_id)
        .join(WardrobeItem, WardrobeItem.id == OutfitItem.item_id)
        .where(
            WearHistory.user_id == user_id,
            WearHistory.worn_date >= w.start,
            WearHistory.worn_date <= w.end,
            WardrobeItem.user_id == user_id,
            WardrobeItem.deleted_at.is_(None),
        )
        .group_by(
            WardrobeItem.id,
            WardrobeItem.title,
            WardrobeItem.category,
        )
        .order_by(wear_count.desc(), WardrobeItem.id)
        .limit(limit)
    )
    return [
        TopItem(
            item_id=r.id,
            title=r.title,
            category=r.category,
            wear_count=int(r.wear_count),
        )
        for r in result.all()
    ]


async def _top_combos(
    session: AsyncSession, user_id: uuid.UUID, w: PeriodWindow, limit: int = 3
) -> list[OutfitCombo]:
    """Most-worn outfit combinations in the window (by WearHistory rows / outfit)."""
    wear_count = func.count().label("wear_count")
    result = await session.execute(
        select(Outfit.id, Outfit.title, wear_count)
        .select_from(WearHistory)
        .join(Outfit, Outfit.id == WearHistory.outfit_id)
        .where(
            WearHistory.user_id == user_id,
            WearHistory.worn_date >= w.start,
            WearHistory.worn_date <= w.end,
        )
        .group_by(Outfit.id, Outfit.title)
        .order_by(wear_count.desc(), Outfit.id)
        .limit(limit)
    )
    return [
        OutfitCombo(outfit_id=r.id, title=r.title, wear_count=int(r.wear_count))
        for r in result.all()
    ]


async def _color_palette(
    session: AsyncSession, user_id: uuid.UUID, w: PeriodWindow
) -> dict[str, int]:
    """Color frequency of items worn in the window (one count per wear)."""
    cnt = func.count().label("cnt")
    result = await session.execute(
        select(WardrobeItem.color_main, cnt)
        .select_from(WearHistory)
        .join(OutfitItem, OutfitItem.outfit_id == WearHistory.outfit_id)
        .join(WardrobeItem, WardrobeItem.id == OutfitItem.item_id)
        .where(
            WearHistory.user_id == user_id,
            WearHistory.worn_date >= w.start,
            WearHistory.worn_date <= w.end,
            WardrobeItem.deleted_at.is_(None),
        )
        .group_by(WardrobeItem.color_main)
        .order_by(cnt.desc(), WardrobeItem.color_main)
    )
    return {r.color_main: int(r.cnt) for r in result.all()}


async def _style_tag_counts(
    session: AsyncSession, user_id: uuid.UUID, start: date, end: date
) -> Counter[str]:
    """Aggregate style_tags across items worn in [start, end].

    style_tags is JSONB (a list); we pull the per-wear tag arrays and tally in
    Python. The row set is already SQL-filtered to the window — only worn rows
    with non-null tags come back.
    """
    result = await session.execute(
        select(WardrobeItem.style_tags)
        .select_from(WearHistory)
        .join(OutfitItem, OutfitItem.outfit_id == WearHistory.outfit_id)
        .join(WardrobeItem, WardrobeItem.id == OutfitItem.item_id)
        .where(
            WearHistory.user_id == user_id,
            WearHistory.worn_date >= start,
            WearHistory.worn_date <= end,
            WardrobeItem.style_tags.isnot(None),
            WardrobeItem.deleted_at.is_(None),
        )
    )
    counter: Counter[str] = Counter()
    for (style_tags,) in result.all():
        if isinstance(style_tags, list):
            counter.update(t for t in style_tags if isinstance(t, str))
    return counter


async def _style_trend(
    session: AsyncSession, user_id: uuid.UUID, w: PeriodWindow
) -> StyleTrend:
    current = await _style_tag_counts(session, user_id, w.start, w.end)
    previous = await _style_tag_counts(session, user_id, w.prev_start, w.prev_end)
    rising = sorted(
        (t for t, c in current.items() if c > previous.get(t, 0)),
        key=lambda t: current[t] - previous.get(t, 0),
        reverse=True,
    )
    fading = sorted(
        (t for t, c in previous.items() if c > current.get(t, 0)),
        key=lambda t: previous[t] - current.get(t, 0),
        reverse=True,
    )
    return StyleTrend(
        period=w.period,
        prev_period=w.prev_period,
        current=dict(current),
        previous=dict(previous),
        rising=rising,
        fading=fading,
    )


async def _highest_cost_per_wear(
    session: AsyncSession, user_id: uuid.UUID
) -> CostPerWearEntry | None:
    """Lifetime cost-per-wear: purchase_price / lifetime wear_count, highest first.

    Lifetime (not period-scoped) on purpose — a high cost-per-wear is the real
    "didn't earn its keep" signal, and period wears would divide-by-zero. Only
    worn, priced items are considered. Computed in SQL via an ORDER BY on the
    ratio so we fetch a single row.
    """
    cpw = (WardrobeItem.purchase_price / cast(WardrobeItem.wear_count, Integer)).label("cpw")
    result = await session.execute(
        select(
            WardrobeItem.id,
            WardrobeItem.title,
            WardrobeItem.purchase_price,
            WardrobeItem.wear_count,
            cpw,
        )
        .where(
            WardrobeItem.user_id == user_id,
            WardrobeItem.deleted_at.is_(None),
            WardrobeItem.purchase_price.isnot(None),
            WardrobeItem.wear_count > 0,
        )
        .order_by(cpw.desc(), WardrobeItem.id)
        .limit(1)
    )
    r = result.first()
    if r is None:
        return None
    return CostPerWearEntry(
        item_id=r.id,
        title=r.title,
        purchase_price=r.purchase_price,
        wear_count=int(r.wear_count),
        cost_per_wear=round(Decimal(str(r.cpw)), 2),
    )


async def _longest_idle_item(
    session: AsyncSession, user_id: uuid.UUID, w: PeriodWindow
) -> IdleEntry | None:
    """Item idle the longest as of period end.

    Ranked by the shared idle definition (last worn, else date added), oldest
    first; see ``insights.idle``.
    """
    result = await session.execute(
        select(
            WardrobeItem.id,
            WardrobeItem.title,
            WardrobeItem.category,
            WardrobeItem.last_worn_at,
            idle_since_expr().label("idle_since"),
        )
        .where(
            WardrobeItem.user_id == user_id,
            WardrobeItem.deleted_at.is_(None),
        )
        .order_by(idle_since_expr().asc(), WardrobeItem.id)
        .limit(1)
    )
    r = result.first()
    if r is None:
        return None
    idle_days = (w.end - r.idle_since).days
    return IdleEntry(
        item_id=r.id,
        title=r.title,
        category=r.category,
        last_worn=r.last_worn_at,
        idle_days=idle_days,
    )


async def _recommendation_adoption_rate(
    session: AsyncSession, user_id: uuid.UUID, w: PeriodWindow
) -> float:
    """Of the current-revision outfits recommended in the window, the fraction worn.

    Denominator keys on ``daily_recommendations.recommendation_date`` within the
    window and ``is_current = true`` (refresh writes superseded revisions; counting
    them would deflate the rate). Numerator is recommended outfits that have a
    matching WearHistory row. Per-outfit, not per-day.
    """
    denom_result = await session.execute(
        select(func.count(Outfit.id.distinct()))
        .select_from(Outfit)
        .join(DailyRecommendation, DailyRecommendation.id == Outfit.daily_recommendation_id)
        .where(
            DailyRecommendation.user_id == user_id,
            DailyRecommendation.is_current.is_(True),
            DailyRecommendation.recommendation_date >= w.start,
            DailyRecommendation.recommendation_date <= w.end,
        )
    )
    denom = int(denom_result.scalar_one() or 0)
    if denom == 0:
        return 0.0

    num_result = await session.execute(
        select(func.count(Outfit.id.distinct()))
        .select_from(Outfit)
        .join(DailyRecommendation, DailyRecommendation.id == Outfit.daily_recommendation_id)
        .join(WearHistory, WearHistory.outfit_id == Outfit.id)
        .where(
            DailyRecommendation.user_id == user_id,
            DailyRecommendation.is_current.is_(True),
            DailyRecommendation.recommendation_date >= w.start,
            DailyRecommendation.recommendation_date <= w.end,
        )
    )
    num = int(num_result.scalar_one() or 0)
    return round(num / denom, 4)


async def build_report(
    user_id: uuid.UUID, session: AsyncSession, period: str
) -> WardrobeReport:
    """Aggregate the full wardrobe report for ``period``.

    Raises FriendlyException (422) on a malformed period. When the user had no
    wears in the window, returns a structurally-complete empty report (zeros /
    nulls / empty lists) rather than erroring — querying a pre-history period is
    the common case.
    """
    w = parse_period(period)

    total_wears, prev_total_wears = await asyncio.gather(
        _count_wears(session, user_id, w.start, w.end),
        _count_wears(session, user_id, w.prev_start, w.prev_end),
    )
    if total_wears == 0:
        return WardrobeReport(
            period=w.period,
            label=w.label,
            start_date=w.start,
            end_date=w.end,
            is_empty=True,
            total_wears=0,
            prev_total_wears=prev_total_wears,
            style_trend=await _style_trend(session, user_id, w),
            highest_cost_per_wear=await _highest_cost_per_wear(session, user_id),
            longest_idle_item=await _longest_idle_item(session, user_id, w),
            recommendation_adoption_rate=await _recommendation_adoption_rate(
                session, user_id, w
            ),
        )

    style_keywords_counter = await _style_tag_counts(session, user_id, w.start, w.end)
    return WardrobeReport(
        period=w.period,
        label=w.label,
        start_date=w.start,
        end_date=w.end,
        is_empty=False,
        total_wears=total_wears,
        prev_total_wears=prev_total_wears,
        top_items=await _top_items(session, user_id, w),
        top_combos=await _top_combos(session, user_id, w),
        color_palette=await _color_palette(session, user_id, w),
        style_keywords=[t for t, _ in style_keywords_counter.most_common(8)],
        style_trend=await _style_trend(session, user_id, w),
        highest_cost_per_wear=await _highest_cost_per_wear(session, user_id),
        longest_idle_item=await _longest_idle_item(session, user_id, w),
        recommendation_adoption_rate=await _recommendation_adoption_rate(
            session, user_id, w
        ),
    )
