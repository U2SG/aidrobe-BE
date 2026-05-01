import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import import_tasks as import_tasks_router
from app.presentation.middlewares.exception_handler import setup_exception_handlers


def build_import_tasks_app(session: AsyncSession) -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)

    async def override_db_session() -> AsyncSession:
        return session

    def override_settings() -> Settings:
        return Settings(APP_ENV="dev", VISION_PROVIDER="fake")

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = override_settings
    app.include_router(import_tasks_router.router)
    return app


async def test_create_import_task_only_accepts_image(db_session: AsyncSession) -> None:
    app = build_import_tasks_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/v1/import-tasks",
            headers={"X-User-Id": str(uuid.uuid4())},
            json={
                "input_type": "url",
                "source_type": "camera",
                "image_url": "https://cdn/item.jpg",
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == 4001


async def test_create_import_task_reaches_waiting_confirm(db_session: AsyncSession) -> None:
    app = build_import_tasks_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/v1/import-tasks",
            headers={"X-User-Id": str(uuid.uuid4())},
            json={
                "input_type": "image",
                "source_type": "camera",
                "image_url": "https://cdn/shirt.jpg",
            },
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "waiting_confirm"
    assert data["candidate_item"]["category"] == "top"
    assert data["candidate_item"]["color_main"] == "white"


async def test_get_import_task_returns_candidate_item(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    app = build_import_tasks_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        create_response = await client.post(
            "/v1/import-tasks",
            headers={"X-User-Id": str(user_id)},
            json={
                "input_type": "image",
                "source_type": "album",
                "image_url": "https://cdn/pants.jpg",
            },
        )
        task_id = create_response.json()["data"]["task_id"]
        get_response = await client.get(
            f"/v1/import-tasks/{task_id}",
            headers={"X-User-Id": str(user_id)},
        )

    assert get_response.status_code == 200
    data = get_response.json()["data"]
    assert data["candidate_item"]["title"] == "Black trousers"
    assert data["candidate_item"]["category"] == "bottom"


async def test_failed_vision_response_stores_failed_task(db_session: AsyncSession) -> None:
    app = build_import_tasks_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/v1/import-tasks",
            headers={"X-User-Id": str(uuid.uuid4())},
            json={
                "input_type": "image",
                "source_type": "camera",
                "image_url": "https://cdn/fail.jpg",
            },
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["status"] == "failed"
    assert data["error_code"] == "vision_failed"


async def test_missing_or_other_user_task_returns_404(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    other_user_id = uuid.uuid4()
    app = build_import_tasks_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        create_response = await client.post(
            "/v1/import-tasks",
            headers={"X-User-Id": str(user_id)},
            json={
                "input_type": "image",
                "source_type": "camera",
                "image_url": "https://cdn/shirt.jpg",
            },
        )
        task_id = create_response.json()["data"]["task_id"]
        response = await client.get(
            f"/v1/import-tasks/{task_id}",
            headers={"X-User-Id": str(other_user_id)},
        )

    assert response.status_code == 404
    assert response.json()["code"] == 4041
