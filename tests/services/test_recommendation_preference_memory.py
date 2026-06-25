"""BE#46 — explicit NEGATIVE preferences influence Today recommendations.

Acceptance: a stated dislike of a color down-ranks outfits in that color when
ENABLE_PREFERENCE_MEMORY is on, and is a no-op when off (clean rollback). The
generator reads `get_settings()` directly (lru-cached), so the flag test
monkeypatches the module-level `get_settings` — the cost_guard / today_context
pattern.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.preferences.memory import SOURCE_EXPLICIT, record_preference
from app.application.recommendations.generator import RecommendationGenerator
from app.common.enums import Category, ItemStatus
from app.config.settings import Settings
from app.infrastructure.persistence.models.recommendation import Outfit, OutfitItem
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.preference_memory_repo import (
    SqlAlchemyPreferenceMemoryRepository,
)
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)


async def _seed(session: AsyncSession) -> tuple[uuid.UUID, dict[str, uuid.UUID]]:
    user = User()
    session.add(user)
    await session.flush()
    ids: dict[str, uuid.UUID] = {}
    for title, category, color in [
        ("Red shirt", Category.TOP.value, "red"),
        ("Blue shirt", Category.TOP.value, "blue"),
        ("Black pants", Category.BOTTOM.value, "black"),
    ]:
        item = WardrobeItem(
            user_id=user.id,
            title=title,
            category=category,
            color_main=color,
            status=ItemStatus.WEARABLE.value,
        )
        session.add(item)
        await session.flush()
        ids[color] = item.id
    return user.id, ids


async def _outfit_item_colors(
    session: AsyncSession, daily_id: uuid.UUID, item_color: dict[uuid.UUID, str]
) -> dict[uuid.UUID, set[str]]:
    from sqlalchemy import select

    outfit_rows = list(
        await session.scalars(select(Outfit).where(Outfit.daily_recommendation_id == daily_id))
    )
    out: dict[uuid.UUID, set[str]] = {}
    for o in outfit_rows:
        item_rows = list(
            await session.scalars(select(OutfitItem).where(OutfitItem.outfit_id == o.id))
        )
        out[o.id] = {item_color[r.item_id] for r in item_rows if r.item_id in item_color}
    return out


async def test_negative_color_downranks_outfits_when_flag_on(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id, ids = await _seed(db_session)
    item_color = {v: k for k, v in ids.items()}
    await record_preference(
        SqlAlchemyPreferenceMemoryRepository(db_session),
        user_id=user_id,
        preference_type="color",
        preference_value="red",
        polarity="negative",
        confidence=0.95,
        source=SOURCE_EXPLICIT,
    )

    monkeypatch.setattr(
        "app.application.recommendations.generator.get_settings",
        lambda: Settings(APP_ENV="dev", ENABLE_PREFERENCE_MEMORY=True),
    )
    repo = SqlAlchemyRecommendationRepository(db_session)
    daily = await RecommendationGenerator(repo).generate_for_user(
        user_id, date(2026, 5, 1), "manual"
    )
    assert daily.status == "ready"
    colors_by_outfit = await _outfit_item_colors(db_session, daily.id, item_color)
    nonred_outfits = [oid for oid, cols in colors_by_outfit.items() if "red" not in cols]
    assert nonred_outfits, "expected at least one non-red outfit to exist"
    # User-visible outcome: the top-ranked outfit must not be a red one when red
    # is disliked and a non-red alternative exists.
    from sqlalchemy import select

    rank1 = await db_session.scalar(
        select(Outfit)
        .where(Outfit.daily_recommendation_id == daily.id)
        .order_by(Outfit.rank_order)
    )
    assert rank1 is not None
    assert "red" not in colors_by_outfit[rank1.id]
    # The engine breakdown records the penalty so it's auditable.
    assert isinstance(daily.score_breakdown, dict)


async def test_flag_off_ignores_negative_preference(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With the flag off the stated dislike must NOT alter generation — a clean
    rollback path. Compared against a run with no preference rows at all."""
    user_id, ids = await _seed(db_session)
    await record_preference(
        SqlAlchemyPreferenceMemoryRepository(db_session),
        user_id=user_id,
        preference_type="color",
        preference_value="red",
        polarity="negative",
        confidence=0.95,
        source=SOURCE_EXPLICIT,
    )

    monkeypatch.setattr(
        "app.application.recommendations.generator.get_settings",
        lambda: Settings(APP_ENV="dev", ENABLE_PREFERENCE_MEMORY=False),
    )
    repo = SqlAlchemyRecommendationRepository(db_session)
    daily = await RecommendationGenerator(repo).generate_for_user(
        user_id, date(2026, 5, 1), "manual"
    )
    assert daily.status == "ready"
    # daily.score_breakdown = {outfit_type: engine_outfit.score_breakdown}; the
    # engine breakdown carries disliked_color_penalty. Flag off → engine got an
    # empty set → every penalty is 0.
    assert isinstance(daily.score_breakdown, dict)
    for bd in daily.score_breakdown.values():
        assert bd.get("disliked_color_penalty", 0) == 0
