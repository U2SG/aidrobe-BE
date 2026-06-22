import asyncio
import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.import_task import ImportTask
from app.infrastructure.persistence.models.wardrobe import WardrobeItem, WardrobeItemImage
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import import_tasks as import_tasks_router
from app.presentation.middlewares.exception_handler import setup_exception_handlers


def build_import_confirm_app(session: AsyncSession) -> FastAPI:
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


async def create_task(client: AsyncClient, user_id: uuid.UUID) -> str:
    response = await client.post(
        "/v1/import-tasks",
        headers={"X-User-Id": str(user_id)},
        json={
            "input_type": "image",
            "source_type": "camera",
            "image_url": "https://cdn/shirt.jpg",
        },
    )
    task_id = response.json()["data"]["task_id"]
    for _ in range(5):
        poll = await client.get(
            f"/v1/import-tasks/{task_id}",
            headers={"X-User-Id": str(user_id)},
        )
        if poll.json()["data"]["status"] == "waiting_confirm":
            return task_id
        await asyncio.sleep(0)
    return task_id


async def test_confirm_waiting_task_creates_wardrobe_item(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    app = build_import_confirm_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        task_id = await create_task(client, user_id)
        response = await client.post(
            f"/v1/import-tasks/{task_id}/confirm",
            headers={"X-User-Id": str(user_id)},
            json={"title": "Office shirt", "category": "top", "color_main": "white"},
        )

    assert response.status_code == 200
    item_id = response.json()["data"]["item_id"]
    item = await db_session.get(WardrobeItem, uuid.UUID(item_id))
    assert item is not None
    assert item.title == "Office shirt"
    images = list(await db_session.scalars(select(WardrobeItemImage)))
    assert {image.image_type for image in images} == {"original", "standard"}


async def test_confirm_accepts_location_and_fit_fields(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    app = build_import_confirm_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        task_id = await create_task(client, user_id)
        response = await client.post(
            f"/v1/import-tasks/{task_id}/confirm",
            headers={"X-User-Id": str(user_id)},
            json={
                "title": "Office shirt",
                "category": "top",
                "color_main": "white",
                "location": "main closet",
                "size_label": "M",
                "fit_status": "fits_well",
            },
        )

    assert response.status_code == 200
    item_id = response.json()["data"]["item_id"]
    item = await db_session.get(WardrobeItem, uuid.UUID(item_id))
    assert item is not None
    assert item.location == "main closet"
    assert item.size_label == "M"
    assert item.fit_status == "fits_well"


async def test_confirm_keeps_ai_naming_and_records_provenance(db_session: AsyncSession) -> None:
    """BE#30 — accepting the AI display name verbatim flags name_source='ai'
    and carries the canonical/localized names onto the item."""
    user_id = uuid.uuid4()
    app = build_import_confirm_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        task_id = await create_task(client, user_id)
        # Default locale is zh-CN, so the candidate display name is "白色衬衫".
        response = await client.post(
            f"/v1/import-tasks/{task_id}/confirm",
            headers={"X-User-Id": str(user_id)},
            json={"title": "白色衬衫", "category": "top", "color_main": "white"},
        )

    assert response.status_code == 200
    item_id = response.json()["data"]["item_id"]
    item = await db_session.get(WardrobeItem, uuid.UUID(item_id))
    assert item is not None
    assert item.title == "白色衬衫"
    assert item.name_source == "ai"
    assert item.canonical_name == "White shirt"
    assert item.localized_names == {"en-US": "White shirt", "zh-CN": "白色衬衫"}


async def test_confirm_user_custom_name_wins(db_session: AsyncSession) -> None:
    """A typed title that differs from the AI display name is kept verbatim
    and flagged name_source='user' — the AI name never overwrites it."""
    user_id = uuid.uuid4()
    app = build_import_confirm_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        task_id = await create_task(client, user_id)
        response = await client.post(
            f"/v1/import-tasks/{task_id}/confirm",
            headers={"X-User-Id": str(user_id)},
            json={"title": "My favourite shirt", "category": "top", "color_main": "white"},
        )

    assert response.status_code == 200
    item_id = response.json()["data"]["item_id"]
    item = await db_session.get(WardrobeItem, uuid.UUID(item_id))
    assert item is not None
    assert item.title == "My favourite shirt"
    assert item.name_source == "user"
    # canonical/localized provenance is still carried from the candidate.
    assert item.canonical_name == "White shirt"


async def test_confirm_completed_task_returns_409(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    app = build_import_confirm_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        task_id = await create_task(client, user_id)
        payload = {"title": "Office shirt", "category": "top", "color_main": "white"}
        first = await client.post(
            f"/v1/import-tasks/{task_id}/confirm",
            headers={"X-User-Id": str(user_id)},
            json=payload,
        )
        second = await client.post(
            f"/v1/import-tasks/{task_id}/confirm",
            headers={"X-User-Id": str(user_id)},
            json=payload,
        )

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["code"] == 4091


async def test_confirm_marks_import_task_completed(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    app = build_import_confirm_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        task_id = await create_task(client, user_id)
        await client.post(
            f"/v1/import-tasks/{task_id}/confirm",
            headers={"X-User-Id": str(user_id)},
            json={"title": "Edited name", "category": "top", "color_main": "blue"},
        )

    task = await db_session.get(ImportTask, uuid.UUID(task_id))
    assert task is not None
    assert task.task_status == "completed"
