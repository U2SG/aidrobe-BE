"""BE#40 — recommendation event log query coverage (repo, no API)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import RecommendationEventType
from app.infrastructure.persistence.models.recommendation import RecommendationEvent
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)


async def _seed_event(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    event_type: str,
    created_at: datetime,
) -> None:
    session.add(
        RecommendationEvent(
            user_id=user_id,
            event_type=event_type,
            source="test",
            created_at=created_at,
        )
    )
    await session.flush()


async def test_list_events_filters_by_time_window_and_type(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    other_user = uuid.uuid4()
    db_session.add_all([User(id=user_id), User(id=other_user)])
    await db_session.flush()

    await _seed_event(
        db_session, user_id, event_type=RecommendationEventType.LIKED.value,
        created_at=datetime(2026, 6, 1, tzinfo=UTC),
    )
    await _seed_event(
        db_session, user_id, event_type=RecommendationEventType.DISLIKED.value,
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
    )
    await _seed_event(
        db_session, user_id, event_type=RecommendationEventType.LIKED.value,
        created_at=datetime(2026, 6, 20, tzinfo=UTC),
    )
    # Another user's event must never leak into the first user's query.
    await _seed_event(
        db_session, other_user, event_type=RecommendationEventType.LIKED.value,
        created_at=datetime(2026, 6, 10, tzinfo=UTC),
    )

    repo = SqlAlchemyRecommendationRepository(db_session)

    # Full window, newest first.
    all_events = await repo.list_recommendation_events(user_id)
    assert len(all_events) == 3
    assert all_events[0].created_at == datetime(2026, 6, 20, tzinfo=UTC)

    # Time window excludes the June-1 and June-20 events.
    windowed = await repo.list_recommendation_events(
        user_id, since=datetime(2026, 6, 5, tzinfo=UTC), until=datetime(2026, 6, 15, tzinfo=UTC)
    )
    assert len(windowed) == 1
    assert windowed[0].event_type == RecommendationEventType.DISLIKED.value

    # Type filter.
    liked = await repo.list_recommendation_events(
        user_id, event_type=RecommendationEventType.LIKED.value
    )
    assert len(liked) == 2
    assert all(e.event_type == RecommendationEventType.LIKED.value for e in liked)
