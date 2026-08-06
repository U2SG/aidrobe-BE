import uuid
from datetime import date

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.recommendations.generator import RecommendationGenerator
from app.common.enums import Category, ItemStatus
from app.infrastructure.persistence.models.recommendation import (
    DailyRecommendation,
    Outfit,
    RecommendationTrace,
)
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
    trace = await db_session.scalar(
        select(RecommendationTrace).where(
            RecommendationTrace.daily_recommendation_id == daily.id
        )
    )
    assert trace is not None
    assert daily.trace is trace
    assert trace.payload["candidate_trace"]["candidates"]
    assert trace.payload["provenance"]["llm"] is None


async def test_generator_persists_insufficient_items(db_session: AsyncSession) -> None:
    user = User()
    db_session.add(user)
    await db_session.flush()

    daily = await RecommendationGenerator(
        SqlAlchemyRecommendationRepository(db_session)
    ).generate_for_user(user.id, date(2026, 5, 1), "manual")

    assert daily.status == "insufficient_items"
    assert daily.trace is not None
    assert daily.trace.payload["candidate_trace"]["status"] == "insufficient_items"


async def test_generator_regenerates_same_user_date(db_session: AsyncSession) -> None:
    user = User()
    db_session.add(user)
    await db_session.flush()
    await add_item(db_session, user.id, "White shirt", Category.TOP.value, "white")
    await add_item(db_session, user.id, "Blue shirt", Category.TOP.value, "blue")
    await add_item(db_session, user.id, "Black pants", Category.BOTTOM.value, "black")
    generator = RecommendationGenerator(SqlAlchemyRecommendationRepository(db_session))

    first = await generator.generate_for_user(user.id, date(2026, 5, 1), "manual")
    assert first.trace is not None
    first_trace_id = first.trace.id
    second = await generator.generate_for_user(user.id, date(2026, 5, 1), "manual")

    assert first.id == second.id
    count = await db_session.scalar(
        select(func.count())
        .select_from(DailyRecommendation)
        .where(DailyRecommendation.user_id == user.id)
    )
    assert count == 1
    assert second.trace is not None
    assert second.trace.id == first_trace_id


async def test_stored_trace_replays_deterministically(db_session: AsyncSession) -> None:
    from evals.recommendations.replay import (
        compare_versions,
        load_replay_input,
        replay_exact,
    )

    user = User()
    db_session.add(user)
    await db_session.flush()
    await add_item(db_session, user.id, "White shirt", Category.TOP.value, "white")
    await add_item(db_session, user.id, "Black pants", Category.BOTTOM.value, "black")
    await add_item(db_session, user.id, "White shoes", Category.SHOES.value, "white")

    daily = await RecommendationGenerator(
        SqlAlchemyRecommendationRepository(db_session)
    ).generate_for_user(user.id, date(2026, 5, 1), "manual")
    assert daily.trace is not None

    replay_input = load_replay_input(daily.trace)
    first = await replay_exact(daily.trace)
    second = await replay_exact(daily.trace)

    assert replay_input.target_date == date(2026, 5, 1)
    assert [outfit.item_ids for outfit in first.outfits] == [
        outfit.item_ids for outfit in second.outfits
    ]
    assert [outfit.score_total for outfit in first.outfits] == [
        outfit.score_total for outfit in second.outfits
    ]
    comparison = await compare_versions(
        daily.trace,
        baseline_parameters=replay_input.parameters,
        candidate_parameters=replay_input.parameters,
    )
    assert comparison.selected_item_diffs == {}
    assert set(comparison.selected_score_deltas.values()) == {0.0}
