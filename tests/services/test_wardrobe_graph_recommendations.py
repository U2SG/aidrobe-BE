import uuid
from datetime import date
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.recommendations.generator import RecommendationGenerator
from app.common.enums import Category, ItemStatus
from app.config.settings import Settings
from app.infrastructure.persistence.models.recommendation import Outfit, OutfitItem
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)
from app.infrastructure.persistence.repositories.wardrobe_relationship_repo import (
    SqlAlchemyWardrobeRelationshipRepository,
)


async def _seed_user_and_items(
    session: AsyncSession,
) -> tuple[uuid.UUID, dict[str, uuid.UUID]]:
    user = User()
    session.add(user)
    await session.flush()
    ids: dict[str, uuid.UUID] = {}
    for key, title, category, color in [
        ("red", "Red shirt", Category.TOP.value, "red"),
        ("blue", "Blue shirt", Category.TOP.value, "blue"),
        ("black", "Black pants", Category.BOTTOM.value, "black"),
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
        ids[key] = item.id
    return user.id, ids


async def _seed_relationships(
    session: AsyncSession,
    ids: dict[str, uuid.UUID],
) -> None:
    repo = SqlAlchemyWardrobeRelationshipRepository(session)
    await repo.upsert_relationship(
        source_item_id=ids["red"],
        target_item_id=ids["black"],
        relationship_type="user_rejected_pairing",
        score=-1.0,
        confidence=1.0,
        source="user_feedback",
    )
    await repo.upsert_relationship(
        source_item_id=ids["blue"],
        target_item_id=ids["black"],
        relationship_type="user_approved_pairing",
        score=1.0,
        confidence=1.0,
        source="user_feedback",
    )


async def _ranked_outfits(session: AsyncSession, daily_id: uuid.UUID) -> list[Outfit]:
    return list(
        await session.scalars(
            select(Outfit)
            .where(Outfit.daily_recommendation_id == daily_id)
            .order_by(Outfit.rank_order)
        )
    )


async def _outfit_item_ids(session: AsyncSession, outfit_id: uuid.UUID) -> set[uuid.UUID]:
    return set(
        await session.scalars(
            select(OutfitItem.item_id).where(OutfitItem.outfit_id == outfit_id)
        )
    )


async def test_wardrobe_graph_boosts_and_penalizes_ranking_when_flag_on(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, ids = await _seed_user_and_items(db_session)
    await _seed_relationships(db_session, ids)
    monkeypatch.setattr(
        "app.application.recommendations.generator.get_settings",
        lambda: Settings(APP_ENV="dev", ENABLE_WARDROBE_GRAPH=True),
    )

    daily = await RecommendationGenerator(
        SqlAlchemyRecommendationRepository(db_session)
    ).generate_for_user(user_id, date(2026, 5, 1), "manual")

    outfits = await _ranked_outfits(db_session, daily.id)
    rank1_item_ids = await _outfit_item_ids(db_session, outfits[0].id)
    assert ids["blue"] in rank1_item_ids
    assert ids["red"] not in rank1_item_ids
    assert isinstance(daily.score_breakdown, dict)
    safe_breakdown: Any = daily.score_breakdown["safe"]
    assert safe_breakdown["wardrobe_graph_boost"] > 0


async def test_wardrobe_graph_does_not_change_ranking_when_flag_off(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id, ids = await _seed_user_and_items(db_session)
    await _seed_relationships(db_session, ids)
    monkeypatch.setattr(
        "app.application.recommendations.generator.get_settings",
        lambda: Settings(APP_ENV="dev", ENABLE_WARDROBE_GRAPH=False),
    )

    daily = await RecommendationGenerator(
        SqlAlchemyRecommendationRepository(db_session)
    ).generate_for_user(user_id, date(2026, 5, 1), "manual")

    outfits = await _ranked_outfits(db_session, daily.id)
    rank1_item_ids = await _outfit_item_ids(db_session, outfits[0].id)
    assert ids["red"] in rank1_item_ids
    assert isinstance(daily.score_breakdown, dict)
    for breakdown in daily.score_breakdown.values():
        assert "wardrobe_graph_boost" not in breakdown
        assert "wardrobe_graph_penalty" not in breakdown
