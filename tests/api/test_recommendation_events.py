"""BE#81 — passive-learning event ingestion (POST /v1/recommendations/events)."""

import uuid

from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.persistence.models.recommendation import (
    OutfitFeedback,
    RecommendationEvent,
)
from app.infrastructure.persistence.models.user import User
from tests.api.test_recommendations_today import build_recommendations_app


async def _seed_user(session: AsyncSession) -> uuid.UUID:
    user_id = uuid.uuid4()
    session.add(User(id=user_id))
    await session.flush()
    return user_id


async def test_ingest_event_persists(db_session: AsyncSession) -> None:
    user_id = await _seed_user(db_session)
    app = build_recommendations_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/recommendations/events",
            headers={"X-User-Id": str(user_id)},
            json={
                "event_type": "temperature_mismatch_reported",
                "signal_strength": "strong",
                "source_surface": "today",
                "metadata": {"reason": "too_cold", "temperatureC": 25},
            },
        )
    assert resp.status_code == 200
    event_id = resp.json()["data"]["event_id"]

    row = await db_session.scalar(
        select(RecommendationEvent).where(RecommendationEvent.id == uuid.UUID(event_id))
    )
    assert row is not None
    assert row.event_type == "temperature_mismatch_reported"
    assert row.signal_strength == "strong"
    assert row.source_surface == "today"
    assert row.event_metadata == {"reason": "too_cold", "temperatureC": 25}


async def test_signal_strengths_stored_distinctly(db_session: AsyncSession) -> None:
    user_id = await _seed_user(db_session)
    app = build_recommendations_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        for strength in ("weak", "medium", "strong"):
            resp = await client.post(
                "/v1/recommendations/events",
                headers={"X-User-Id": str(user_id)},
                json={
                    "event_type": "outfit_impression",
                    "signal_strength": strength,
                    "source_surface": "today",
                },
            )
            assert resp.status_code == 200

    strengths = (
        await db_session.scalars(
            select(RecommendationEvent.signal_strength).where(
                RecommendationEvent.user_id == user_id
            )
        )
    ).all()
    assert sorted(strengths) == ["medium", "strong", "weak"]


async def test_weak_signal_does_not_write_explicit_feedback(db_session: AsyncSession) -> None:
    # AC: weak inferred signals must not overwrite explicit preferences. Ingestion
    # only appends to the event stream — it must never create an OutfitFeedback row.
    user_id = await _seed_user(db_session)
    app = build_recommendations_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/recommendations/events",
            headers={"X-User-Id": str(user_id)},
            json={
                "event_type": "outfit_disliked",
                "signal_strength": "weak",
                "source_surface": "today",
                "outfit_id": str(uuid.uuid4()),
            },
        )
    assert resp.status_code == 200
    feedback_count = await db_session.scalar(
        select(func.count()).select_from(OutfitFeedback).where(OutfitFeedback.user_id == user_id)
    )
    assert feedback_count == 0


async def test_replay_appends_independently(db_session: AsyncSession) -> None:
    # Append-only: replaying the same event is safe (no crash) and each call is a
    # distinct row — the stream is raw material, dedup happens at aggregation time.
    user_id = await _seed_user(db_session)
    app = build_recommendations_app(db_session)
    body = {
        "event_type": "resurface_insight_viewed",
        "signal_strength": "weak",
        "source_surface": "today",
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        for _ in range(2):
            resp = await client.post(
                "/v1/recommendations/events",
                headers={"X-User-Id": str(user_id)},
                json=body,
            )
            assert resp.status_code == 200

    count = await db_session.scalar(
        select(func.count()).select_from(RecommendationEvent).where(
            RecommendationEvent.user_id == user_id
        )
    )
    assert count == 2


async def test_invalid_event_type_rejected(db_session: AsyncSession) -> None:
    user_id = await _seed_user(db_session)
    app = build_recommendations_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/recommendations/events",
            headers={"X-User-Id": str(user_id)},
            json={"event_type": "worn", "signal_strength": "weak", "source_surface": "today"},
        )
    assert resp.status_code == 422


async def test_invalid_source_surface_rejected(db_session: AsyncSession) -> None:
    user_id = await _seed_user(db_session)
    app = build_recommendations_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/recommendations/events",
            headers={"X-User-Id": str(user_id)},
            json={
                "event_type": "outfit_impression",
                "signal_strength": "weak",
                "source_surface": "settings",
            },
        )
    assert resp.status_code == 422
