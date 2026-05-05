import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import style as style_router
from app.presentation.middlewares.exception_handler import setup_exception_handlers


def build_style_app(session: AsyncSession) -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)

    async def override_db_session() -> AsyncSession:
        return session

    def override_settings() -> Settings:
        return Settings(APP_ENV="dev")

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = override_settings
    app.include_router(style_router.router)
    return app


async def test_add_inspiration_with_image_url_returns_queued(db_session: AsyncSession) -> None:
    app = build_style_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            "/v1/style/inspirations",
            headers={"X-User-Id": str(uuid.uuid4())},
            json={"image_url": "https://cdn.example.com/outfit.jpg"},
        )
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["learn_status"] == "queued"
    assert data["image_url"] == "https://cdn.example.com/outfit.jpg"


async def test_add_inspiration_with_source_url_returns_queued(db_session: AsyncSession) -> None:
    app = build_style_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            "/v1/style/inspirations",
            headers={"X-User-Id": str(uuid.uuid4())},
            json={"source_url": "https://www.xiaohongshu.com/explore/abc123"},
        )
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["learn_status"] == "queued"
    assert data["source_url"] == "https://www.xiaohongshu.com/explore/abc123"


async def test_add_inspiration_requires_at_least_one_url(db_session: AsyncSession) -> None:
    app = build_style_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            "/v1/style/inspirations",
            headers={"X-User-Id": str(uuid.uuid4())},
            json={},
        )
    assert response.status_code == 422
    assert response.json()["code"] == 4001


async def test_batch_add_inspirations(db_session: AsyncSession) -> None:
    app = build_style_app(db_session)
    urls = [
        "https://cdn.example.com/style1.jpg",
        "https://cdn.example.com/style2.jpg",
        "https://cdn.example.com/style3.jpg",
    ]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            "/v1/style/sources/batch",
            headers={"X-User-Id": str(uuid.uuid4())},
            json={"urls": urls},
        )
    assert response.status_code == 201
    items = response.json()["data"]["items"]
    assert len(items) == 3
    assert all(item["learn_status"] == "queued" for item in items)
