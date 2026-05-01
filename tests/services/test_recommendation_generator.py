import uuid
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.recommendations.generator import RecommendationGenerator
from app.common.enums import Category, ItemStatus
from app.infrastructure.persistence.models.recommendation import DailyRecommendation, Outfit
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)


async def add_item(
    session: AsyncSession, user_id: uuid.UUID, title: str, category: str, color: str
) -> None:
    session.add(
        WardrobeItem(
            user_id=user_id,
            title=title,
            category=category,
            color_main=color,
            status=ItemStatus.WEARABLE.value,
        )
    )
    await session.flush()


async def test_generator_creates_ready_recommendation(db_session: AsyncSession) -> None:
    user = User()
    db_session.add(user)
    await db_session.flush()
    await add_item(db_session, user.id, "White shirt", Category.TOP.value, "white")
    await add_item(db_session, user.id, "Blue shirt", Category.TOP.value, "blue")
    await add_item(db_session, user.id, "Black pants", Category.BOTTOM.value, "black")

    daily = await RecommendationGenerator(
        SqlAlchemyRecommendationRepository(db_session)
    ).generate_for_user(user.id, date(2026, 5, 1), "manual")

    assert daily.status == "ready"
    outfits = list(await db_session.scalars(select(Outfit)))
    assert outfits


async def test_generator_persists_insufficient_items(db_session: AsyncSession) -> None:
    user = User()
    db_session.add(user)
    await db_session.flush()

    daily = await RecommendationGenerator(
        SqlAlchemyRecommendationRepository(db_session)
    ).generate_for_user(user.id, date(2026, 5, 1), "manual")

    assert daily.status == "insufficient_items"


async def test_generator_regenerates_same_user_date(db_session: AsyncSession) -> None:
    user = User()
    db_session.add(user)
    await db_session.flush()
    await add_item(db_session, user.id, "White shirt", Category.TOP.value, "white")
    await add_item(db_session, user.id, "Blue shirt", Category.TOP.value, "blue")
    await add_item(db_session, user.id, "Black pants", Category.BOTTOM.value, "black")
    generator = RecommendationGenerator(SqlAlchemyRecommendationRepository(db_session))

    first = await generator.generate_for_user(user.id, date(2026, 5, 1), "manual")
    second = await generator.generate_for_user(user.id, date(2026, 5, 1), "manual")

    assert first.id == second.id
    count = await db_session.scalar(
        select(func.count())
        .select_from(DailyRecommendation)
        .where(DailyRecommendation.user_id == user.id)
    )
    assert count == 1
