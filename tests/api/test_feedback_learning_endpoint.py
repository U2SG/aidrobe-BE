"""BE#67 — repeated outfit feedback writes a controlled weak preference; a
one-off does not. Exercises the real endpoint + learning write path."""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.outfits import feedback as feedback_module
from app.common.enums import ItemStatus, RecommendationStatus
from app.config.settings import Settings
from app.infrastructure.persistence.models.preference_memory import UserPreferenceMemory
from app.infrastructure.persistence.models.recommendation import (
    DailyRecommendation,
    Outfit,
    OutfitFeedback,
    OutfitItem,
)
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from tests.api.test_outfit_actions import build_outfit_app


async def _seed_outfit(session: AsyncSession, user_id: uuid.UUID) -> uuid.UUID:
    session.add(User(id=user_id))
    await session.flush()
    now = datetime.now(UTC)
    top = WardrobeItem(
        user_id=user_id, title="Blazer", category="top", color_main="navy",
        status=ItemStatus.WEARABLE.value,
    )
    session.add(top)
    await session.flush()
    daily = DailyRecommendation(
        user_id=user_id,
        recommendation_date=date(2026, 6, 26),
        status=RecommendationStatus.READY.value,
        generation_source="test",
        score_breakdown=None,
    )
    session.add(daily)
    await session.flush()
    outfit = Outfit(
        daily_recommendation_id=daily.id,
        user_id=user_id,
        outfit_type="safe",
        title="Test outfit",
        reason_text="seed",
        score_total=Decimal("1.0"),
        rank_order=1,
        created_at=now,
    )
    session.add(outfit)
    await session.flush()
    session.add(OutfitItem(outfit_id=outfit.id, item_id=top.id, role="top", display_order=1, created_at=now))
    await session.flush()
    return outfit.id


async def _formality_prefs(session: AsyncSession, user_id: uuid.UUID) -> list[UserPreferenceMemory]:
    rows = await session.scalars(
        select(UserPreferenceMemory).where(
            UserPreferenceMemory.user_id == user_id,
            UserPreferenceMemory.preference_type == "formality",
        )
    )
    return list(rows)


async def test_repeated_feedback_writes_weak_preference_but_one_off_does_not(
    db_session: AsyncSession, monkeypatch
) -> None:
    # Flag gates the WRITE; patch the consuming module's lru_cached get_settings.
    monkeypatch.setattr(
        feedback_module,
        "get_settings",
        lambda: Settings(APP_ENV="dev", ENABLE_FEEDBACK_LEARNING=True),
    )
    user_id = uuid.uuid4()
    outfit_id = await _seed_outfit(db_session, user_id)
    app = build_outfit_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        async def dislike() -> None:
            resp = await client.post(
                f"/v1/recommendations/outfits/{outfit_id}/feedback",
                headers={"X-User-Id": str(user_id)},
                json={"feedback_type": "dislike", "reason_tags": ["too_formal"]},
            )
            assert resp.status_code == 200

        # One-off (and a second) stay below the durable threshold (3): no preference.
        await dislike()
        await dislike()
        assert await _formality_prefs(db_session, user_id) == []

        # The third repeat crosses the threshold → a single weak preference row.
        await dislike()
        prefs = await _formality_prefs(db_session, user_id)
        assert len(prefs) == 1
        pref = prefs[0]
        assert pref.preference_value == "too_formal"
        assert pref.polarity == "negative"
        assert pref.source == "feedback_signal"
        assert float(pref.confidence) <= 0.4  # weak-source cap honored


async def test_preference_write_db_error_does_not_fail_feedback(
    db_session: AsyncSession, monkeypatch
) -> None:
    """BE#69 review: a non-PreferenceMemoryError during the weak-pref write (e.g. a
    DBAPI error) must be swallowed via the per-row savepoint — the user's feedback
    still persists and the request stays 200, not 500."""
    monkeypatch.setattr(
        feedback_module,
        "get_settings",
        lambda: Settings(APP_ENV="dev", ENABLE_FEEDBACK_LEARNING=True),
    )

    async def _boom(*args, **kwargs):  # noqa: ANN002, ANN003
        raise RuntimeError("simulated DBAPI failure")

    monkeypatch.setattr(feedback_module, "record_preference", _boom)

    user_id = uuid.uuid4()
    outfit_id = await _seed_outfit(db_session, user_id)
    app = build_outfit_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        for _ in range(3):  # cross the durable threshold so the write path runs
            resp = await client.post(
                f"/v1/recommendations/outfits/{outfit_id}/feedback",
                headers={"X-User-Id": str(user_id)},
                json={"feedback_type": "dislike", "reason_tags": ["too_formal"]},
            )
            assert resp.status_code == 200  # learning error never surfaces as 500

    # No preference was written (every attempt failed), but feedback persisted.
    assert await _formality_prefs(db_session, user_id) == []
    persisted = await db_session.scalar(
        select(OutfitFeedback).where(OutfitFeedback.outfit_id == outfit_id)
    )
    assert persisted is not None and persisted.feedback_type == "dislike"
