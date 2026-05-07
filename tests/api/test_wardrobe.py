import asyncio
import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import import_tasks as import_tasks_router
from app.presentation.api.v1.routers import wardrobe as wardrobe_router
from app.presentation.middlewares.exception_handler import setup_exception_handlers


def build_wardrobe_app(session: AsyncSession) -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)

    async def override_db_session() -> AsyncSession:
        return session

    def override_settings() -> Settings:
        return Settings(APP_ENV="dev", VISION_PROVIDER="fake")

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = override_settings
    app.include_router(import_tasks_router.router)
    app.include_router(wardrobe_router.router)
    return app


async def create_item(
    client: AsyncClient, user_id: uuid.UUID, image_url: str = "https://cdn/shirt.jpg"
) -> str:
    create_response = await client.post(
        "/v1/import-tasks",
        headers={"X-User-Id": str(user_id)},
        json={"input_type": "image", "source_type": "camera", "image_url": image_url},
    )
    task = create_response.json()["data"]
    for _ in range(5):
        poll = await client.get(
            f"/v1/import-tasks/{task['task_id']}",
            headers={"X-User-Id": str(user_id)},
        )
        task = poll.json()["data"]
        if task["status"] == "waiting_confirm":
            break
        await asyncio.sleep(0)
    candidate = task["candidate_item"]
    confirm_response = await client.post(
        f"/v1/import-tasks/{task['task_id']}/confirm",
        headers={"X-User-Id": str(user_id)},
        json={
            "title": candidate["title"],
            "category": candidate["category"],
            "color_main": candidate["color_main"],
        },
    )
    return confirm_response.json()["data"]["item_id"]


async def test_list_wardrobe_items_returns_total_and_pagination(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    app = build_wardrobe_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        await create_item(client, user_id, "https://cdn/shirt.jpg")
        await create_item(client, user_id, "https://cdn/pants.jpg")
        response = await client.get(
            "/v1/wardrobe-items?limit=1&offset=0",
            headers={"X-User-Id": str(user_id)},
        )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["total"] == 2
    assert len(data["items"]) == 1


async def test_get_wardrobe_item_enforces_ownership(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    other_user_id = uuid.uuid4()
    app = build_wardrobe_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        item_id = await create_item(client, user_id)
        response = await client.get(
            f"/v1/wardrobe-items/{item_id}",
            headers={"X-User-Id": str(other_user_id)},
        )

    assert response.status_code == 404
    assert response.json()["code"] == 4041


async def test_patch_wardrobe_item_updates_fields(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    app = build_wardrobe_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        item_id = await create_item(client, user_id)
        response = await client.patch(
            f"/v1/wardrobe-items/{item_id}",
            headers={"X-User-Id": str(user_id)},
            json={
                "title": "Updated",
                "category": "top",
                "color_main": "blue",
                "status": "unavailable",
            },
        )

    assert response.status_code == 200
    assert response.json()["data"]["title"] == "Updated"
    assert response.json()["data"]["status"] == "unavailable"


async def test_patch_wardrobe_item_rejects_invalid_status(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    app = build_wardrobe_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        item_id = await create_item(client, user_id)
        response = await client.patch(
            f"/v1/wardrobe-items/{item_id}",
            headers={"X-User-Id": str(user_id)},
            json={"status": "laundry"},
        )

    assert response.status_code == 422
    assert response.json()["code"] == 4001


async def test_delete_wardrobe_item_soft_deletes(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    app = build_wardrobe_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        item_id = await create_item(client, user_id)
        delete_response = await client.delete(
            f"/v1/wardrobe-items/{item_id}",
            headers={"X-User-Id": str(user_id)},
        )
        list_response = await client.get("/v1/wardrobe-items", headers={"X-User-Id": str(user_id)})

    assert delete_response.status_code == 200
    assert delete_response.json()["data"] == {"deleted": True}
    assert list_response.json()["data"]["total"] == 0



@pytest.mark.parametrize("status", ["wearable", "unavailable", "dirty", "lost", "donated"])
async def test_patch_wardrobe_item_accepts_all_valid_statuses(db_session: AsyncSession, status: str) -> None:
    user_id = uuid.uuid4()
    app = build_wardrobe_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        item_id = await create_item(client, user_id)
        response = await client.patch(
            f"/v1/wardrobe-items/{item_id}",
            headers={"X-User-Id": str(user_id)},
            json={"status": status},
        )

    assert response.status_code == 200
    assert response.json()["data"]["status"] == status


async def test_patch_wardrobe_item_rejects_garbage_status(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    app = build_wardrobe_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        item_id = await create_item(client, user_id)
        response = await client.patch(
            f"/v1/wardrobe-items/{item_id}",
            headers={"X-User-Id": str(user_id)},
            json={"status": "garbage"},
        )

    assert response.status_code == 422


async def test_patch_wardrobe_item_purchase_date_round_trip(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    app = build_wardrobe_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        item_id = await create_item(client, user_id)
        patch_resp = await client.patch(
            f"/v1/wardrobe-items/{item_id}",
            headers={"X-User-Id": str(user_id)},
            json={"purchase_date": "2026-04-01"},
        )
        get_resp = await client.get(
            f"/v1/wardrobe-items/{item_id}",
            headers={"X-User-Id": str(user_id)},
        )

    assert patch_resp.status_code == 200
    assert patch_resp.json()["data"]["purchase_date"] == "2026-04-01"
    assert get_resp.status_code == 200
    assert get_resp.json()["data"]["purchase_date"] == "2026-04-01"


async def test_last_worn_surfaces_from_db(db_session: AsyncSession) -> None:
    from datetime import date

    from sqlalchemy import select

    from app.infrastructure.persistence.models.wardrobe import WardrobeItem

    user_id = uuid.uuid4()
    app = build_wardrobe_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        item_id = await create_item(client, user_id)

    stmt = select(WardrobeItem).where(WardrobeItem.id == uuid.UUID(item_id))
    result = await db_session.execute(stmt)
    item = result.scalar_one()
    item.last_worn_at = date(2026, 4, 15)
    await db_session.flush()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        get_resp = await client.get(
            f"/v1/wardrobe-items/{item_id}",
            headers={"X-User-Id": str(user_id)},
        )

    assert get_resp.status_code == 200
    assert get_resp.json()["data"]["last_worn"] == "2026-04-15"
