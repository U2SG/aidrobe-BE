import asyncio
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import (
    import_tasks,
    recommendations,
    settings,
    uploads,
    wardrobe,
)
from app.presentation.middlewares.exception_handler import setup_exception_handlers


def build_flow_app(session: AsyncSession) -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)

    async def override_db_session() -> AsyncSession:
        return session

    def override_settings() -> Settings:
        return Settings(APP_ENV="dev")

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = override_settings
    for router in [
        settings.router,
        uploads.router,
        import_tasks.router,
        wardrobe.router,
        recommendations.router,
    ]:
        app.include_router(router)
    return app


async def add_item(client: AsyncClient, user_id: uuid.UUID, image_url: str) -> str:
    task_response = await client.post(
        "/v1/import-tasks",
        headers={"X-User-Id": str(user_id)},
        json={"input_type": "image", "source_type": "camera", "image_url": image_url},
    )
    task = task_response.json()["data"]
    for _ in range(20):
        poll = await client.get(
            f"/v1/import-tasks/{task['task_id']}",
            headers={"X-User-Id": str(user_id)},
        )
        task = poll.json()["data"]
        if task["status"] == "waiting_confirm":
            break
        await asyncio.sleep(0)
    candidate = task.get("candidate_item") or {}
    confirm = await client.post(
        f"/v1/import-tasks/{task['task_id']}/confirm",
        headers={"X-User-Id": str(user_id)},
        json={
            "title": candidate.get("title", "Test Item"),
            "category": candidate.get("category", "top"),
            "color_main": candidate.get("color_main", "black"),
        },
    )
    return confirm.json()["data"]["item_id"]


async def test_v1_slim_full_flow(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    app = build_flow_app(db_session)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        await client.patch(
            "/v1/user/settings",
            headers={"X-User-Id": str(user_id)},
            json={"departure_time": "08:30:00"},
        )
        presign = await client.post(
            "/v1/uploads/presign",
            json={"file_name": "shirt.jpg", "content_type": "image/jpeg"},
        )
        assert presign.status_code == 200

        await add_item(client, user_id, "https://cdn/shirt.jpg")
        await add_item(client, user_id, "https://cdn/blue-shirt.jpg")
        await add_item(client, user_id, "https://cdn/pants.jpg")

        today = await client.get("/v1/recommendations/today", headers={"X-User-Id": str(user_id)})
        outfit_id = today.json()["data"]["outfits"][0]["outfit_id"]
        wear = await client.post(
            f"/v1/recommendations/outfits/{outfit_id}/wear",
            headers={"X-User-Id": str(user_id)},
            json={"worn_date": "2026-05-01"},
        )
        feedback = await client.post(
            f"/v1/recommendations/outfits/{outfit_id}/feedback",
            headers={"X-User-Id": str(user_id)},
            json={"feedback_type": "like"},
        )

    assert today.json()["data"]["status"] == "ready"
    assert wear.status_code == 200
    assert feedback.status_code == 200


async def test_insufficient_items_flow_never_returns_generating(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    app = build_flow_app(db_session)

    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get(
            "/v1/recommendations/today", headers={"X-User-Id": str(user_id)}
        )

    assert response.json()["data"]["status"] == "insufficient_items"
