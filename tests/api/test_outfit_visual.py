"""Tests for V3 M10: outfit visual assets endpoint."""

import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.outfit_visual import OutfitVisualAsset
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import outfit_visual as visual_router
from app.presentation.middlewares.exception_handler import setup_exception_handlers


def build_visual_app(session: AsyncSession) -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)

    async def override_db() -> AsyncSession:
        return session

    def override_settings() -> Settings:
        return Settings(APP_ENV="dev")

    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[get_settings] = override_settings
    app.include_router(visual_router.router)
    return app


async def test_get_outfit_visual_asset_queued(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    db_session.add(User(id=user_id))
    await db_session.flush()
    asset = OutfitVisualAsset(user_id=user_id, generation_status="queued", visual_type="outfit_board")
    db_session.add(asset)
    await db_session.flush()
    app = build_visual_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get(
            f"/v1/outfit-visual-assets/{asset.id}",
            headers={"X-User-Id": str(user_id)},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["generation_status"] == "queued"
    assert body["image_url"] is None


async def test_get_outfit_visual_asset_completed(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    db_session.add(User(id=user_id))
    await db_session.flush()
    asset = OutfitVisualAsset(
        user_id=user_id,
        generation_status="completed",
        image_url="https://cdn.example.com/board.jpg",
    )
    db_session.add(asset)
    await db_session.flush()
    app = build_visual_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get(
            f"/v1/outfit-visual-assets/{asset.id}",
            headers={"X-User-Id": str(user_id)},
        )

    assert resp.status_code == 200
    assert resp.json()["image_url"] == "https://cdn.example.com/board.jpg"


async def test_get_outfit_visual_asset_not_found(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    db_session.add(User(id=user_id))
    await db_session.flush()
    app = build_visual_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get(
            f"/v1/outfit-visual-assets/{uuid.uuid4()}",
            headers={"X-User-Id": str(user_id)},
        )

    assert resp.status_code == 404


async def test_get_outfit_visual_asset_wrong_user(db_session: AsyncSession) -> None:
    owner_id = uuid.uuid4()
    other_id = uuid.uuid4()
    db_session.add(User(id=owner_id))
    db_session.add(User(id=other_id))
    await db_session.flush()
    asset = OutfitVisualAsset(user_id=owner_id, generation_status="completed")
    db_session.add(asset)
    await db_session.flush()
    app = build_visual_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get(
            f"/v1/outfit-visual-assets/{asset.id}",
            headers={"X-User-Id": str(other_id)},
        )

    assert resp.status_code == 404
