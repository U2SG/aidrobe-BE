"""Tests for V3 M12: wardrobe insights endpoints."""

import uuid
from datetime import datetime, timezone

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import insights as insights_router
from app.presentation.middlewares.exception_handler import setup_exception_handlers


def build_insights_app(session: AsyncSession) -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)

    async def override_db() -> AsyncSession:
        return session

    def override_settings() -> Settings:
        return Settings(APP_ENV="dev")

    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[get_settings] = override_settings
    app.include_router(insights_router.router)
    return app


async def seed_wardrobe(session: AsyncSession, user_id: uuid.UUID) -> list[WardrobeItem]:
    session.add(User(id=user_id))
    items = [
        WardrobeItem(user_id=user_id, title="White shirt", category="top", color_main="white", wear_count=5),
        WardrobeItem(user_id=user_id, title="Black pants", category="bottom", color_main="black", wear_count=3),
        WardrobeItem(
            user_id=user_id,
            title="Old coat",
            category="outerwear",
            color_main="gray",
            wear_count=0,
            created_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        ),
    ]
    for i in items:
        session.add(i)
    await session.flush()
    return items


async def test_insights_summary(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    await seed_wardrobe(db_session, user_id)
    app = build_insights_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get("/v1/insights/summary", headers={"X-User-Id": str(user_id)})

    assert resp.status_code == 200
    data = resp.json()
    assert data["total_items"] == 3
    assert data["idle_count"] == 1
    assert data["active_rate"] > 0
    assert data["most_worn_category"] in ("top", "bottom")  # tie-break is DB-dependent
    assert data["favorite_color"] in ("white", "black")


async def test_insights_top_items(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    await seed_wardrobe(db_session, user_id)
    app = build_insights_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get("/v1/insights/top-items", headers={"X-User-Id": str(user_id)})

    assert resp.status_code == 200
    items = resp.json()
    assert items[0]["title"] == "White shirt"
    assert items[0]["wear_count"] == 5


async def test_insights_idle_items(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    await seed_wardrobe(db_session, user_id)
    app = build_insights_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get("/v1/insights/idle-items", headers={"X-User-Id": str(user_id)})

    assert resp.status_code == 200
    items = resp.json()
    assert len(items) == 1
    assert items[0]["title"] == "Old coat"


async def test_insights_color_palette(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    await seed_wardrobe(db_session, user_id)
    app = build_insights_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get("/v1/insights/color-palette", headers={"X-User-Id": str(user_id)})

    assert resp.status_code == 200
    palette = resp.json()["palette"]
    assert "white" in palette
    assert "black" in palette


async def test_insights_cost_per_wear(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    session = db_session
    session.add(User(id=user_id))
    item = WardrobeItem(
        user_id=user_id, title="Expensive coat", category="outerwear",
        color_main="navy", wear_count=10, purchase_price=500,
    )
    session.add(item)
    await session.flush()
    app = build_insights_app(session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get("/v1/insights/cost-per-wear", headers={"X-User-Id": str(user_id)})

    assert resp.status_code == 200
    rows = resp.json()
    assert len(rows) == 1
    assert float(rows[0]["cost_per_wear"]) == 50.0


async def test_insights_empty_wardrobe(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    db_session.add(User(id=user_id))
    await db_session.flush()
    app = build_insights_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get("/v1/insights/summary", headers={"X-User-Id": str(user_id)})

    assert resp.status_code == 200
    assert resp.json()["total_items"] == 0
