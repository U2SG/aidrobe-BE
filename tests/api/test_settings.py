import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import settings as settings_router
from app.presentation.middlewares.exception_handler import setup_exception_handlers


def build_settings_app(session: AsyncSession) -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)

    async def override_db_session() -> AsyncSession:
        return session

    def override_settings() -> Settings:
        return Settings(APP_ENV="dev")

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = override_settings
    app.include_router(settings_router.router)
    return app


async def test_get_user_settings_returns_departure_time(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    app = build_settings_app(db_session)
    headers = {"X-User-Id": str(user_id)}

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        patch_response = await client.patch(
            "/v1/user/settings", headers=headers, json={"departure_time": "08:30:00"}
        )
        get_response = await client.get("/v1/user/settings", headers=headers)

    assert patch_response.status_code == 200
    assert get_response.status_code == 200
    data = get_response.json()["data"]
    assert data["departure_time"] == "08:30:00"


async def test_patch_user_settings_updates_departure_time(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    app = build_settings_app(db_session)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.patch(
            "/v1/user/settings",
            headers={"X-User-Id": str(user_id)},
            json={"departure_time": "07:45:00"},
        )

    assert response.status_code == 200
    assert response.json()["data"]["departure_time"] == "07:45:00"


async def test_patch_user_settings_rejects_invalid_time(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    app = build_settings_app(db_session)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.patch(
            "/v1/user/settings",
            headers={"X-User-Id": str(user_id)},
            json={"departure_time": "not-a-time"},
        )

    assert response.status_code == 422
    assert response.json()["code"] == 4001
