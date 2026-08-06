"""Phase D3 — POST /v1/recommendations/today/refresh."""

from __future__ import annotations

import uuid
from datetime import date, datetime, time, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.recommendations.generator import RecommendationGenerator
from app.common.enums import Category, FeedbackType, ItemStatus, RecommendationStatus
from app.common.local_time import local_datetime, utc_now
from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.recommendation import (
    DailyRecommendation,
    Outfit,
    OutfitFeedback,
    RecommendationEvent,
)
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import recommendations as recommendations_router
from app.presentation.middlewares.exception_handler import setup_exception_handlers


def _build_app(
    session: AsyncSession,
    settings_override: Settings,
    monkeypatch: pytest.MonkeyPatch | None = None,
) -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)

    async def override_db_session() -> AsyncSession:
        return session

    def override_settings() -> Settings:
        return settings_override

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = override_settings
    # The refresh_today use case calls get_settings() directly (bypasses
    # FastAPI dep). Match the codebase convention (cost_guard /
    # user_purge_worker) and patch the module-level get_settings.
    if monkeypatch is not None:
        monkeypatch.setattr(
            "app.application.recommendations.refresh_today.get_settings",
            lambda: settings_override,
        )
        monkeypatch.setattr(
            recommendations_router, "get_settings", lambda: settings_override
        )
    app.include_router(recommendations_router.router)
    return app


async def _seed_user_with_wardrobe(session: AsyncSession) -> uuid.UUID:
    user_id = uuid.uuid4()
    session.add(User(id=user_id))
    await session.flush()
    for title, category, color in [
        ("White shirt", Category.TOP.value, "white"),
        ("Blue shirt", Category.TOP.value, "blue"),
        ("Black pants", Category.BOTTOM.value, "black"),
    ]:
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
    return user_id


async def _generate_initial_revision(session: AsyncSession, user_id: uuid.UUID) -> None:
    await RecommendationGenerator(SqlAlchemyRecommendationRepository(session)).generate_for_user(
        user_id, _local_today(), "nightly_fallback"
    )


def _local_today() -> date:
    return local_datetime(utc_now(), None, Settings().DEFAULT_TIMEZONE).date()


async def test_refresh_endpoint_returns_403_when_flag_off(db_session: AsyncSession) -> None:
    user_id = await _seed_user_with_wardrobe(db_session)
    await _generate_initial_revision(db_session, user_id)

    app = _build_app(db_session, Settings(APP_ENV="dev", ENABLE_TODAY_REFRESH=False))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/recommendations/today/refresh", headers={"X-User-Id": str(user_id)}
        )
    assert resp.status_code == 403


async def test_refresh_writes_new_revision_and_archives_old(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Refresh must NOT overwrite — old revision stays accessible by id so
    wear/feedback references that point at it remain valid."""
    user_id = await _seed_user_with_wardrobe(db_session)
    await _generate_initial_revision(db_session, user_id)

    repo = SqlAlchemyRecommendationRepository(db_session)
    rev1 = await repo.get_daily(user_id, _local_today())
    assert rev1 is not None
    rev1_id = rev1.id
    assert rev1.revision == 1
    assert rev1.is_current is True

    app = _build_app(
        db_session, Settings(APP_ENV="dev", ENABLE_TODAY_REFRESH=True), monkeypatch
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/recommendations/today/refresh", headers={"X-User-Id": str(user_id)}
        )
    assert resp.status_code == 200, resp.text

    db_session.expire_all()
    rev2 = await repo.get_daily(user_id, _local_today())
    assert rev2 is not None
    assert rev2.id != rev1_id
    assert rev2.revision == 2
    assert rev2.is_current is True

    # Old revision still exists, just archived.
    archived = await db_session.get(DailyRecommendation, rev1_id)
    assert archived is not None
    assert archived.is_current is False
    assert archived.revision == 1


async def test_refresh_response_carries_new_active_identity_and_revision(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = await _seed_user_with_wardrobe(db_session)
    await _generate_initial_revision(db_session, user_id)
    repository = SqlAlchemyRecommendationRepository(db_session)
    previous = await repository.get_daily(user_id, _local_today())
    assert previous is not None

    app = _build_app(
        db_session, Settings(APP_ENV="dev", ENABLE_TODAY_REFRESH=True), monkeypatch
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/v1/recommendations/today/refresh", headers={"X-User-Id": str(user_id)}
        )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    active = await repository.get_daily(user_id, _local_today())
    assert active is not None
    assert data["recommendation_id"] == str(active.id)
    assert data["recommendation_id"] != str(previous.id)
    assert data["revision"] == active.revision == 2
    assert data["generated_at"]
    assert data["generation_source"] == "refresh"
    assert data["trace_id"]
    assert previous.trace is not None
    assert data["trace_id"] != str(previous.trace.id)


async def test_refresh_rate_limit_returns_429_after_exhaustion(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = await _seed_user_with_wardrobe(db_session)
    await _generate_initial_revision(db_session, user_id)

    app = _build_app(
        db_session,
        Settings(APP_ENV="dev", ENABLE_TODAY_REFRESH=True, TODAY_REFRESH_DAILY_LIMIT=2),
        monkeypatch,
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        # Limit is 2 total rows. Initial gen counts as 1; one refresh should
        # succeed (now 2 total); the next must be rejected.
        first = await client.post(
            "/v1/recommendations/today/refresh", headers={"X-User-Id": str(user_id)}
        )
        assert first.status_code == 200
        second = await client.post(
            "/v1/recommendations/today/refresh", headers={"X-User-Id": str(user_id)}
        )
        assert second.status_code == 429


async def test_refresh_preserves_old_outfit_feedback_via_outfit_id(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Detail endpoint by outfit_id must still resolve the old outfit after
    refresh, because feedback rows reference the OLD outfit_id and the user
    may want to look back at the outfit they actually wore."""
    user_id = await _seed_user_with_wardrobe(db_session)
    await _generate_initial_revision(db_session, user_id)
    repo = SqlAlchemyRecommendationRepository(db_session)
    rev1 = await repo.get_daily(user_id, _local_today())
    assert rev1 is not None
    old_outfit = sorted(rev1.outfits, key=lambda o: o.rank_order)[0]

    db_session.add(
        OutfitFeedback(
            outfit_id=old_outfit.id,
            user_id=user_id,
            feedback_type=FeedbackType.LIKE.value,
            reason_tags=[],
            created_at=datetime.now() - timedelta(minutes=1),
        )
    )
    await db_session.commit()

    app = _build_app(
        db_session, Settings(APP_ENV="dev", ENABLE_TODAY_REFRESH=True), monkeypatch
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        refresh_resp = await client.post(
            "/v1/recommendations/today/refresh", headers={"X-User-Id": str(user_id)}
        )
        assert refresh_resp.status_code == 200

        detail = await client.get(
            f"/v1/recommendations/outfits/{old_outfit.id}",
            headers={"X-User-Id": str(user_id)},
        )
        # Old outfit still reachable for the user — references stay valid.
        assert detail.status_code == 200
        assert detail.json()["data"]["outfit_id"] == str(old_outfit.id)


async def test_refresh_disabled_use_case_does_not_create_revision(
    db_session: AsyncSession,
) -> None:
    """When the flag is off the use case must short-circuit BEFORE writing
    anything — no half-state on the table."""
    user_id = await _seed_user_with_wardrobe(db_session)
    await _generate_initial_revision(db_session, user_id)

    app = _build_app(db_session, Settings(APP_ENV="dev", ENABLE_TODAY_REFRESH=False))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/recommendations/today/refresh", headers={"X-User-Id": str(user_id)}
        )
    assert resp.status_code == 403

    repo = SqlAlchemyRecommendationRepository(db_session)
    count = await repo.count_revisions_for_date(user_id, _local_today())
    assert count == 1  # only the initial gen


async def test_known_context_override_is_persisted_and_attributed(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = await _seed_user_with_wardrobe(db_session)
    await _generate_initial_revision(db_session, user_id)
    settings = Settings(
        APP_ENV="dev",
        ENABLE_TODAY_REFRESH=True,
        ENABLE_CONTEXT_OVERRIDES=True,
        ENABLE_TODAY_CONTEXT=True,
    )
    app = _build_app(db_session, settings, monkeypatch)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/v1/recommendations/today/refresh",
            headers={"X-User-Id": str(user_id)},
            json={
                "context_override": {
                    "source": "chip",
                    "chip_id": "noSkirt",
                }
            },
        )

    assert response.status_code == 200, response.text
    data = response.json()["data"]
    assert data["recommendation_id"]
    assert data["revision"] == 2
    assert data["generation_source"] == "context_override"
    assert data["applied_context"]["constraints"] == ["exclude_dress"]
    assert data["context"]["constraints"] == ["exclude_dress"]

    daily = await SqlAlchemyRecommendationRepository(db_session).get_daily(
        user_id, _local_today()
    )
    assert daily is not None
    assert daily.context_json is not None
    assert daily.context_json["constraints"] == ["exclude_dress"]
    assert daily.trace is not None
    assert data["trace_id"] == str(daily.trace.id)
    assert daily.trace.payload["context"]["normalized_constraints"] == [
        "exclude_dress"
    ]
    event = await db_session.scalar(
        select(RecommendationEvent).where(
            RecommendationEvent.user_id == user_id,
            RecommendationEvent.event_type == "context_override_submitted",
        )
    )
    assert event is not None
    assert event.recommendation_id == daily.id


async def test_unapplied_free_text_override_returns_422_without_fake_success(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = await _seed_user_with_wardrobe(db_session)
    await _generate_initial_revision(db_session, user_id)
    settings = Settings(
        APP_ENV="dev",
        ENABLE_TODAY_REFRESH=True,
        ENABLE_CONTEXT_OVERRIDES=True,
    )
    app = _build_app(db_session, settings, monkeypatch)
    monkeypatch.setattr(recommendations_router, "llm_service", lambda: None)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/v1/recommendations/today/refresh",
            headers={"X-User-Id": str(user_id)},
            json={
                "context_override": {
                    "source": "free_text",
                    "text": "今天想穿得特别一点",
                }
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == 4001
    assert await SqlAlchemyRecommendationRepository(db_session).count_revisions_for_date(
        user_id, _local_today()
    ) == 1


# Wide imports to keep module-level fixture resolution stable on partial runs.
_ = (RecommendationStatus, time, Outfit)
