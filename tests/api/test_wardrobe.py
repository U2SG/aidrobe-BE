import asyncio
import uuid
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import ImageType
from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.user import User, UserSettings
from app.infrastructure.persistence.models.wardrobe import WardrobeItem, WardrobeItemImage
from app.infrastructure.persistence.session import get_db_session
from app.infrastructure.vision.fake_vision import FakeVision
from app.presentation.api.v1.routers import import_tasks as import_tasks_router
from app.presentation.api.v1.routers import wardrobe as wardrobe_router
from app.presentation.middlewares.exception_handler import setup_exception_handlers

pytestmark = pytest.mark.usefixtures("import_api_environment")


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


async def test_manual_wardrobe_photo_actions_keep_the_confirmed_item(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.application.wardrobe.item_photo import ItemReference

    reference_url = "https://storage.example/pants.jpg"

    async def reference(*args: object) -> ItemReference:
        return ItemReference(reference_url, None)

    monkeypatch.setattr(wardrobe_router, "item_reference", reference)
    monkeypatch.setattr(wardrobe_router, "vision_service", FakeVision)
    monkeypatch.setattr(wardrobe_router, "get_settings", lambda: Settings(ENABLE_ITEM_CUTOUT=True))
    user_id = uuid.uuid4()
    db_session.add(User(id=user_id))
    await db_session.flush()
    item = WardrobeItem(
        user_id=user_id, title="Confirmed name", category="bottom", color_main="olive",
        original_image_url=reference_url, cover_image_url="https://storage.example/old-product.png",
        cutout_status="ready", created_at=datetime.now(UTC),
    )
    db_session.add(item)
    await db_session.flush()
    path = f"/v1/wardrobe-items/{item.id}"
    headers = {"X-User-Id": str(user_id)}
    async with AsyncClient(transport=ASGITransport(app=build_wardrobe_app(db_session)),
                           base_url="http://testserver") as client:
        suggestion = await client.post(f"{path}/recognize", headers=headers)
        assert suggestion.status_code == 200, suggestion.text
        assert suggestion.json()["data"]["category"] == "bottom"
        assert suggestion.json()["data"]["title"] != item.title
        queued = await client.post(f"{path}/product-image", headers=headers)
        repeated = await client.post(f"{path}/product-image", headers=headers)
        assert queued.status_code == repeated.status_code == 202, queued.text
        for response in (queued, repeated):
            current = response.json()["data"]
            assert current["cutout_status"] == "queued"
            assert current["title"] == "Confirmed name"
            assert current["cover_image_url"] == "https://storage.example/old-product.png"
        for action in ("recognize", "product-image"):
            foreign = await client.post(f"{path}/{action}", headers={"X-User-Id": str(uuid.uuid4())})
            assert foreign.status_code == 404
    assert await db_session.scalar(select(func.count()).select_from(AsyncJob).where(
        AsyncJob.target_id == item.id, AsyncJob.job_type == "cutout",
    )) == 1


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


async def test_wardrobe_resigns_private_images_after_the_original_urls_expire(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    owner_id = uuid.uuid4()
    original_key = f"users/{owner_id}/upload/coat.jpg"
    crop_key = f"{owner_id}/items/crop/cutout.png"
    original_url = f"https://storage.example/aidrobe/{original_key}?Expires=1"
    crop_url = f"https://storage.example/aidrobe/{crop_key}?Expires=1"
    cfg = Settings(
        STORAGE_PROVIDER="s3",
        S3_BUCKET="aidrobe",
        S3_PUBLIC_BASE_URL="https://storage.example/aidrobe",
    )
    monkeypatch.setattr(
        "app.infrastructure.persistence.repositories.wardrobe_repo.get_settings", lambda: cfg,
    )
    signed: list[str] = []

    class PrivateStorage:
        def sign_render_url(self, key: str) -> str:
            signed.append(key)
            return f"https://signed.example/{key}?generation={len(signed)}"

    monkeypatch.setattr("app.bootstrap.container.object_storage", PrivateStorage)
    db_session.add(User(id=owner_id))
    await db_session.flush()
    item = WardrobeItem(
        user_id=owner_id,
        title="Coat",
        category="outerwear",
        color_main="navy",
        original_image_url=original_url,
        cover_image_url=crop_url,
    )
    db_session.add(item)
    await db_session.flush()
    db_session.add(
        WardrobeItemImage(
            item_id=item.id,
            image_type=ImageType.ORIGINAL.value,
            image_url=original_url,
            object_key=original_key,
            created_at=datetime.now(UTC),
        )
    )
    await db_session.flush()
    headers = {"X-User-Id": str(owner_id)}
    async with AsyncClient(transport=ASGITransport(app=build_wardrobe_app(db_session)), base_url="http://test") as c:
        detail = await c.get(f"/v1/wardrobe-items/{item.id}", headers=headers)
        listed = await c.get("/v1/wardrobe-items", headers=headers)

    assert (detail.status_code, listed.status_code) == (200, 200)
    first = detail.json()["data"]
    second = listed.json()["data"]["items"][0]
    assert first["original_image_url"] == f"https://signed.example/{original_key}?generation=1"
    assert first["cover_image_url"] == f"https://signed.example/{crop_key}?generation=2"
    assert second["original_image_url"] == f"https://signed.example/{original_key}?generation=3"
    assert second["cover_image_url"] == f"https://signed.example/{crop_key}?generation=4"
    assert item.original_image_url == original_url
    assert item.cover_image_url == crop_url


async def test_wardrobe_detail_and_list_include_resolved_display_name(
    db_session: AsyncSession,
) -> None:
    user_id = uuid.uuid4()
    db_session.add(User(id=user_id))
    item = WardrobeItem(
        user_id=user_id,
        title="Sneaker",
        category="shoes",
        color_main="white",
        status="wearable",
        name_source="ai",
        localized_names=None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db_session.add(item)
    await db_session.flush()

    app = build_wardrobe_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        detail = await client.get(
            f"/v1/wardrobe-items/{item.id}",
            headers={"X-User-Id": str(user_id)},
            params={"locale": "zh-CN"},
        )
        listing = await client.get(
            "/v1/wardrobe-items",
            headers={"X-User-Id": str(user_id)},
            params={"locale": "en-US"},
        )

    assert detail.status_code == 200, detail.text
    assert detail.json()["data"]["display_name"] == "白色运动鞋"
    assert listing.status_code == 200, listing.text
    assert listing.json()["data"]["items"][0]["display_name"] == "White sneakers"


async def test_wardrobe_display_name_uses_user_settings_locale(
    db_session: AsyncSession,
) -> None:
    user_id = uuid.uuid4()
    db_session.add(User(id=user_id))
    db_session.add(UserSettings(user_id=user_id, locale="en-US"))
    item = WardrobeItem(
        user_id=user_id,
        title="Sneaker",
        category="shoes",
        color_main="white",
        status="wearable",
        name_source="ai",
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    db_session.add(item)
    await db_session.flush()

    app = build_wardrobe_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.get(
            f"/v1/wardrobe-items/{item.id}",
            headers={"X-User-Id": str(user_id)},
        )

    assert response.status_code == 200, response.text
    assert response.json()["data"]["display_name"] == "White sneakers"


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
    assert response.json()["data"]["display_name"] == "Updated"
    assert response.json()["data"]["status"] == "unavailable"
    # BE#30 — a user rename takes ownership of the display name.
    assert response.json()["data"]["name_source"] == "user"


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
