import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.recommendation import (
    DailyRecommendation,
    Outfit,
    OutfitItem,
    WearHistory,
)
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import insights as insights_router
from app.presentation.middlewares.exception_handler import setup_exception_handlers


def _build_app(session: AsyncSession, settings_override: Settings) -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)

    async def override_db_session() -> AsyncSession:
        return session

    def override_settings() -> Settings:
        return settings_override

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = override_settings
    app.include_router(insights_router.router)
    return app


@pytest.fixture
def patched_settings_in_router(monkeypatch: pytest.MonkeyPatch):
    """The insights router calls get_settings() directly inside the handler,
    so feature-flag tests must patch the consuming module, not only FastAPI's
    dependency override."""

    def _factory(**overrides: Any) -> Settings:
        merged = Settings(APP_ENV="dev", **overrides)
        monkeypatch.setattr(
            "app.presentation.api.v1.routers.insights.get_settings",
            lambda: merged,
        )
        return merged

    return _factory


async def _add_wear(
    session: AsyncSession,
    user_id: uuid.UUID,
    item_id: uuid.UUID,
    worn_date: date,
) -> None:
    daily = DailyRecommendation(
        user_id=user_id,
        recommendation_date=worn_date,
        status="ready",
        generation_source="manual",
    )
    session.add(daily)
    await session.flush()

    outfit = Outfit(
        daily_recommendation_id=daily.id,
        user_id=user_id,
        outfit_type="safe",
        title=f"Outfit {worn_date.isoformat()}",
        reason_text="Seeded test outfit",
        score_total=Decimal("0.8"),
        rank_order=1,
        created_at=datetime.now(UTC),
    )
    session.add(outfit)
    await session.flush()

    session.add(
        OutfitItem(
            outfit_id=outfit.id,
            item_id=item_id,
            role="item",
            display_order=0,
            created_at=datetime.now(UTC),
        )
    )
    session.add(
        WearHistory(
            user_id=user_id,
            outfit_id=outfit.id,
            worn_date=worn_date,
            selected_at=datetime.now(UTC),
            created_at=datetime.now(UTC),
        )
    )
    await session.flush()


async def _seed_health_data(session: AsyncSession, user_id: uuid.UUID) -> None:
    session.add(User(id=user_id))
    items = [
        WardrobeItem(user_id=user_id, title="White tee", category="top", color_main="white"),
        WardrobeItem(user_id=user_id, title="Blue shirt", category="top", color_main="blue"),
        WardrobeItem(user_id=user_id, title="Knit top", category="top", color_main="cream"),
        WardrobeItem(user_id=user_id, title="Black pants", category="bottom", color_main="black"),
        WardrobeItem(user_id=user_id, title="Canvas shoes", category="shoes", color_main="white"),
    ]
    for item in items:
        session.add(item)
    await session.flush()

    today = date.today()
    await _add_wear(session, user_id, items[0].id, today - timedelta(days=30))
    await _add_wear(session, user_id, items[3].id, today - timedelta(days=10))
    await _add_wear(session, user_id, items[1].id, today - timedelta(days=100))
    await _add_wear(session, user_id, items[4].id, today - timedelta(days=170))


async def _get_health(
    session: AsyncSession,
    user_id: uuid.UUID,
    settings: Settings,
) -> dict[str, Any]:
    app = _build_app(session, settings)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get(
            "/v1/insights/wardrobe-health", headers={"X-User-Id": str(user_id)}
        )
    assert resp.status_code == 200
    return resp.json()


async def test_wardrobe_health_wear_coverage_ratio(
    db_session: AsyncSession, patched_settings_in_router: Any
) -> None:
    user_id = uuid.uuid4()
    await _seed_health_data(db_session, user_id)
    settings = patched_settings_in_router(ENABLE_WARDROBE_HEALTH=True)

    data = await _get_health(db_session, user_id, settings)

    assert data["score"] == 50
    coverage = {item["type"]: item for item in data["insights"]}["wear_coverage"]
    assert coverage["severity"] == "high"
    assert coverage["title"] == "40% of your wardrobe was worn in the last 90 days"


async def test_wardrobe_health_unused_item_counts(
    db_session: AsyncSession, patched_settings_in_router: Any
) -> None:
    user_id = uuid.uuid4()
    await _seed_health_data(db_session, user_id)
    settings = patched_settings_in_router(ENABLE_WARDROBE_HEALTH=True)

    data = await _get_health(db_session, user_id, settings)

    unused = {item["type"]: item for item in data["insights"]}["unused_items"]
    assert unused["severity"] == "high"
    assert unused["title"] == "Unused items: 3 in 60d, 3 in 90d, 1 in 180d"


async def test_wardrobe_health_category_balance(
    db_session: AsyncSession, patched_settings_in_router: Any
) -> None:
    user_id = uuid.uuid4()
    await _seed_health_data(db_session, user_id)
    settings = patched_settings_in_router(ENABLE_WARDROBE_HEALTH=True)

    data = await _get_health(db_session, user_id, settings)

    balance = {item["type"]: item for item in data["insights"]}["category_balance"]
    assert balance["severity"] == "low"
    assert balance["title"] == "Top makes up 60% of your wardrobe"


async def test_wardrobe_health_empty_wardrobe_returns_partial_response(
    db_session: AsyncSession, patched_settings_in_router: Any
) -> None:
    user_id = uuid.uuid4()
    db_session.add(User(id=user_id))
    await db_session.flush()
    settings = patched_settings_in_router(ENABLE_WARDROBE_HEALTH=True)

    data = await _get_health(db_session, user_id, settings)

    assert data == {
        "score": 0,
        "level": "low",
        "summary": "Add a few wardrobe items to unlock a health score.",
        "insights": [
            {
                "type": "data_sparsity",
                "severity": "low",
                "title": "No wardrobe items are available yet",
                "action": "Start with a few everyday pieces, then check back for richer insights.",
            }
        ],
    }


async def test_wardrobe_health_flag_off_returns_503(
    db_session: AsyncSession, patched_settings_in_router: Any
) -> None:
    user_id = uuid.uuid4()
    await _seed_health_data(db_session, user_id)
    settings = patched_settings_in_router(ENABLE_WARDROBE_HEALTH=False)
    app = _build_app(db_session, settings)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get(
            "/v1/insights/wardrobe-health", headers={"X-User-Id": str(user_id)}
        )

    assert resp.status_code == 503
