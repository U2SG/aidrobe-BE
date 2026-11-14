import uuid
from datetime import date, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings, get_settings
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


async def _seed_health_data(session: AsyncSession, user_id: uuid.UUID) -> None:
    """Seed via the authoritative ``WardrobeItem.last_worn_at`` signal.

    This mirrors the production path: ``WearOutfit.execute`` forward-updates
    ``last_worn_at`` on every recorded wear, and wardrobe_health reads that
    column directly. One item is left never-worn (last_worn_at NULL) to exercise
    graceful partial-data handling.

    Resulting signal over 5 items (3 top / 1 bottom / 1 shoes):
    - worn in last 90d: 2 (30d, 10d) → coverage 40%
    - unused (last worn, else date added): 60d → 2, 90d → 2, 180d → 0; the
      never-worn item was just added, so it is not unused yet
    """
    session.add(User(id=user_id))
    today = date.today()
    items = [
        WardrobeItem(
            user_id=user_id, title="White tee", category="top", color_main="white",
            last_worn_at=today - timedelta(days=30),
        ),
        WardrobeItem(
            user_id=user_id, title="Blue shirt", category="top", color_main="blue",
            last_worn_at=today - timedelta(days=100),
        ),
        WardrobeItem(
            user_id=user_id, title="Knit top", category="top", color_main="cream",
            last_worn_at=None,
        ),
        WardrobeItem(
            user_id=user_id, title="Black pants", category="bottom", color_main="black",
            last_worn_at=today - timedelta(days=10),
        ),
        WardrobeItem(
            user_id=user_id, title="Canvas shoes", category="shoes", color_main="white",
            last_worn_at=today - timedelta(days=170),
        ),
    ]
    for item in items:
        session.add(item)
    await session.flush()


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
    assert data["level"] == "fair"
    coverage = {item["type"]: item for item in data["insights"]}["wear_coverage"]
    assert coverage["severity"] == "high"
    assert coverage["metrics"] == {"coverage_ratio": 0.4, "window_days": 90}


async def test_wardrobe_health_unused_item_counts(
    db_session: AsyncSession, patched_settings_in_router: Any
) -> None:
    user_id = uuid.uuid4()
    await _seed_health_data(db_session, user_id)
    settings = patched_settings_in_router(ENABLE_WARDROBE_HEALTH=True)

    data = await _get_health(db_session, user_id, settings)

    unused = {item["type"]: item for item in data["insights"]}["unused_items"]
    assert unused["severity"] == "medium"
    assert unused["metrics"] == {"unused_60": 2, "unused_90": 2, "unused_180": 0}


async def test_wardrobe_health_category_balance(
    db_session: AsyncSession, patched_settings_in_router: Any
) -> None:
    user_id = uuid.uuid4()
    await _seed_health_data(db_session, user_id)
    settings = patched_settings_in_router(ENABLE_WARDROBE_HEALTH=True)

    data = await _get_health(db_session, user_id, settings)

    balance = {item["type"]: item for item in data["insights"]}["category_balance"]
    assert balance["severity"] == "low"
    assert balance["metrics"] == {
        "dominant_ratio": 0.6,
        "counts": {"top": 3, "bottom": 1, "shoes": 1},
    }


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
        "insights": [
            {
                "type": "data_sparsity",
                "severity": "low",
                "metrics": {"item_count": 0},
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
