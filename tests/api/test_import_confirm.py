import asyncio
import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.import_task import ImportTask
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem, WardrobeItemImage
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import import_tasks as import_tasks_router
from app.presentation.middlewares.exception_handler import setup_exception_handlers


pytestmark = pytest.mark.usefixtures("import_api_environment")


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


async def test_confirm_retry_returns_the_same_item(db_session: AsyncSession) -> None:
    # A retried confirm (lost response, double tap) replays the first result
    # instead of failing with 409 or creating a second item.
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
    assert second.status_code == 200
    assert second.json()["data"]["item_id"] == first.json()["data"]["item_id"]
    items = list(await db_session.scalars(select(WardrobeItem).where(WardrobeItem.user_id == user_id)))
    assert len(items) == 1


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


# --- BE#25 batch confirm-items -------------------------------------------


async def seed_multi_candidate_task(
    session: AsyncSession, user_id: uuid.UUID
) -> ImportTask:
    """Seed a waiting_confirm task with two candidates: tmp_1 (raw photo only)
    and tmp_2 (carrying a crop + cutout url so we can assert image preference).

    Seeds the owning User first — the import-task FK requires it, and unlike the
    endpoint path nothing has called get_or_create(user_id) yet."""
    session.add(User(id=user_id))
    await session.flush()
    task = ImportTask(
        user_id=user_id,
        input_type="image_batch",
        source_type="album",
        task_status="waiting_confirm",
        input_payload={"image_url": "https://cdn/batch.jpg"},
        candidate_payload={
            "candidate_items": [
                {
                    "temp_item_id": "tmp_1",
                    "title": "白色衬衫",
                    "display_name": "白色衬衫",
                    "canonical_name": "White shirt",
                    "localized_names": {"en-US": "White shirt", "zh-CN": "白色衬衫"},
                    "category": "top",
                    "color_main": "white",
                    "confidence_score": 0.9,
                    "original_image_url": "https://cdn/shirt.jpg",
                    "should_import": True,
                },
                {
                    "temp_item_id": "tmp_2",
                    "title": "蓝色牛仔裤",
                    "display_name": "蓝色牛仔裤",
                    "category": "bottom",
                    "color_main": "blue",
                    "confidence_score": 0.8,
                    "original_image_url": "https://cdn/jeans-orig.jpg",
                    "crop_image_url": "https://cdn/jeans-crop.jpg",
                    "cutout_image_url": "https://cdn/jeans-cutout.jpg",
                    "should_import": True,
                },
            ],
        },
    )
    session.add(task)
    await session.flush()
    return task


async def test_confirm_items_creates_multiple_wardrobe_items(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    task = await seed_multi_candidate_task(db_session, user_id)
    app = build_import_confirm_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            f"/v1/import-tasks/{task.id}/confirm-items",
            headers={"X-User-Id": str(user_id)},
            json={
                "items": [
                    {"temp_item_id": "tmp_1", "title": "白色衬衫", "category": "top", "color_main": "white"},
                    {
                        "temp_item_id": "tmp_2",
                        "title": "My jeans",
                        "category": "bottom",
                        "color_main": "blue",
                        "location": "drawer",
                    },
                ]
            },
        )

    assert response.status_code == 200
    created = response.json()["data"]["created_items"]
    assert {c["temp_item_id"] for c in created} == {"tmp_1", "tmp_2"}

    by_temp = {c["temp_item_id"]: c["item_id"] for c in created}
    item1 = await db_session.get(WardrobeItem, uuid.UUID(by_temp["tmp_1"]))
    item2 = await db_session.get(WardrobeItem, uuid.UUID(by_temp["tmp_2"]))
    assert item1 is not None and item2 is not None
    # tmp_1: AI name accepted verbatim -> provenance carried, name_source 'ai'.
    assert item1.name_source == "ai"
    assert item1.canonical_name == "White shirt"
    assert item1.original_image_url == "https://cdn/shirt.jpg"
    assert item1.cover_image_url == "https://cdn/shirt.jpg"
    # tmp_2: user-typed title -> 'user'; cover prefers cutout over crop/original.
    assert item2.name_source == "user"
    assert item2.location == "drawer"
    assert item2.original_image_url == "https://cdn/jeans-orig.jpg"
    assert item2.cover_image_url == "https://cdn/jeans-cutout.jpg"

    # task is now completed
    await db_session.refresh(task)
    assert task.task_status == "completed"


async def test_confirm_items_image_rows_prefer_cutout_for_standard(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    task = await seed_multi_candidate_task(db_session, user_id)
    app = build_import_confirm_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            f"/v1/import-tasks/{task.id}/confirm-items",
            headers={"X-User-Id": str(user_id)},
            json={"items": [{"temp_item_id": "tmp_2", "title": "Jeans", "category": "bottom", "color_main": "blue"}]},
        )

    assert response.status_code == 200
    item_id = uuid.UUID(response.json()["data"]["created_items"][0]["item_id"])
    images = list(await db_session.scalars(select(WardrobeItemImage).where(WardrobeItemImage.item_id == item_id)))
    by_type = {img.image_type: img.image_url for img in images}
    assert by_type == {
        "original": "https://cdn/jeans-orig.jpg",
        "standard": "https://cdn/jeans-cutout.jpg",
        "crop": "https://cdn/jeans-crop.jpg",
    }


async def test_confirm_items_empty_list_returns_422(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    task = await seed_multi_candidate_task(db_session, user_id)
    app = build_import_confirm_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            f"/v1/import-tasks/{task.id}/confirm-items",
            headers={"X-User-Id": str(user_id)},
            json={"items": []},
        )
    assert response.status_code == 422


async def test_confirm_items_unknown_temp_id_rejected(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    task = await seed_multi_candidate_task(db_session, user_id)
    app = build_import_confirm_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            f"/v1/import-tasks/{task.id}/confirm-items",
            headers={"X-User-Id": str(user_id)},
            json={"items": [{"temp_item_id": "nope", "title": "X", "category": "top", "color_main": "white"}]},
        )
    assert response.status_code == 422
    # nothing was created
    items = list(await db_session.scalars(select(WardrobeItem)))
    assert items == []


async def test_confirm_items_duplicate_temp_id_rejected(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    task = await seed_multi_candidate_task(db_session, user_id)
    app = build_import_confirm_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            f"/v1/import-tasks/{task.id}/confirm-items",
            headers={"X-User-Id": str(user_id)},
            json={
                "items": [
                    {"temp_item_id": "tmp_1", "title": "A", "category": "top", "color_main": "white"},
                    {"temp_item_id": "tmp_1", "title": "B", "category": "top", "color_main": "white"},
                ]
            },
        )
    assert response.status_code == 422
    items = list(await db_session.scalars(select(WardrobeItem)))
    assert items == []


async def test_confirm_items_other_user_returns_404(db_session: AsyncSession) -> None:
    owner_id = uuid.uuid4()
    other_id = uuid.uuid4()
    task = await seed_multi_candidate_task(db_session, owner_id)
    app = build_import_confirm_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            f"/v1/import-tasks/{task.id}/confirm-items",
            headers={"X-User-Id": str(other_id)},
            json={"items": [{"temp_item_id": "tmp_1", "title": "X", "category": "top", "color_main": "white"}]},
        )
    assert response.status_code == 404


async def test_confirm_items_non_waiting_returns_409(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    task = await seed_multi_candidate_task(db_session, user_id)
    task.task_status = "completed"
    await db_session.flush()
    app = build_import_confirm_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            f"/v1/import-tasks/{task.id}/confirm-items",
            headers={"X-User-Id": str(user_id)},
            json={"items": [{"temp_item_id": "tmp_1", "title": "X", "category": "top", "color_main": "white"}]},
        )
    assert response.status_code == 409


def _item_payload(temp_id: str, title: str) -> dict[str, str]:
    return {"temp_item_id": temp_id, "title": title, "category": "top", "color_main": "white"}


async def test_create_with_same_idempotency_key_returns_same_task(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    app = build_import_confirm_app(db_session)
    body = {"input_type": "image", "source_type": "camera", "image_url": "https://cdn/shirt.jpg"}
    headers = {"X-User-Id": str(user_id), "Idempotency-Key": "upload-abc"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        first = await client.post("/v1/import-tasks", headers=headers, json=body)
        second = await client.post("/v1/import-tasks", headers=headers, json=body)
        other = await client.post(
            "/v1/import-tasks", headers={**headers, "Idempotency-Key": "upload-def"}, json=body
        )

    assert first.status_code == second.status_code == 202
    assert second.json()["data"]["task_id"] == first.json()["data"]["task_id"]
    assert other.json()["data"]["task_id"] != first.json()["data"]["task_id"]
    tasks = list(await db_session.scalars(select(ImportTask).where(ImportTask.user_id == user_id)))
    assert len(tasks) == 2


async def test_confirm_items_in_batches_keeps_task_open_until_all_confirmed(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    task = await seed_multi_candidate_task(db_session, user_id)
    app = build_import_confirm_app(db_session)
    url = f"/v1/import-tasks/{task.id}/confirm-items"
    headers = {"X-User-Id": str(user_id)}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        first = await client.post(url, headers=headers, json={"items": [_item_payload("tmp_1", "Tee")]})
        polled = await client.get(f"/v1/import-tasks/{task.id}", headers=headers)
        second = await client.post(url, headers=headers, json={"items": [_item_payload("tmp_2", "Skirt")]})
        retry = await client.post(url, headers=headers, json={"items": [_item_payload("tmp_1", "Tee")]})

    assert first.status_code == 200
    assert first.json()["data"]["task_status"] == "waiting_confirm"
    first_item_id = first.json()["data"]["created_items"][0]["item_id"]
    candidates = {c["temp_item_id"]: c for c in polled.json()["data"]["candidate_items"]}
    assert polled.json()["data"]["status"] == "waiting_confirm"
    assert candidates["tmp_1"]["confirmed_item_id"] == first_item_id
    assert candidates["tmp_2"]["confirmed_item_id"] is None

    assert second.status_code == 200
    assert second.json()["data"]["task_status"] == "completed"
    # Re-sending a confirmed candidate after completion replays it.
    assert retry.status_code == 200
    assert retry.json()["data"]["created_items"][0]["item_id"] == first_item_id
    items = list(await db_session.scalars(select(WardrobeItem).where(WardrobeItem.user_id == user_id)))
    assert len(items) == 2


async def test_confirm_items_dismiss_remaining_completes_task(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    task = await seed_multi_candidate_task(db_session, user_id)
    app = build_import_confirm_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            f"/v1/import-tasks/{task.id}/confirm-items",
            headers={"X-User-Id": str(user_id)},
            json={"items": [_item_payload("tmp_1", "Tee")], "dismiss_remaining": True},
        )
        late = await client.post(
            f"/v1/import-tasks/{task.id}/confirm-items",
            headers={"X-User-Id": str(user_id)},
            json={"items": [_item_payload("tmp_2", "Skirt")]},
        )

    assert response.status_code == 200
    assert response.json()["data"]["task_status"] == "completed"
    # A dismissed candidate can no longer be imported.
    assert late.status_code == 409


async def test_confirm_enqueues_item_embedding(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    app = build_import_confirm_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        task_id = await create_task(client, user_id)
        response = await client.post(
            f"/v1/import-tasks/{task_id}/confirm",
            headers={"X-User-Id": str(user_id)},
            json={"title": "Office shirt", "category": "top", "color_main": "white"},
        )

    item_id = uuid.UUID(response.json()["data"]["item_id"])
    jobs = list(
        await db_session.scalars(
            select(AsyncJob).where(AsyncJob.job_type == "embedding", AsyncJob.user_id == user_id)
        )
    )
    assert len(jobs) == 1
    assert jobs[0].payload["item_id"] == str(item_id)


async def test_confirm_copies_recognised_attributes_onto_the_item(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    app = build_import_confirm_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        task_id = await create_task(client, user_id)
        polled = await client.get(f"/v1/import-tasks/{task_id}", headers={"X-User-Id": str(user_id)})
        response = await client.post(
            f"/v1/import-tasks/{task_id}/confirm",
            headers={"X-User-Id": str(user_id)},
            json={"title": "Office shirt", "category": "top", "color_main": "white"},
        )

    candidate = polled.json()["data"]["candidate_item"]
    assert candidate["formality_score"] == 50
    assert candidate["season_tags"] == ["spring", "summer", "autumn"]
    item = await db_session.get(WardrobeItem, uuid.UUID(response.json()["data"]["item_id"]))
    assert item is not None
    assert item.formality_score == 50
    assert item.season_tags == {"tags": ["spring", "summer", "autumn"]}
    assert item.material == "cotton"
    assert item.style_tags == ["minimal", "classic"]


async def test_correcting_the_category_drops_recognised_attributes(db_session: AsyncSession) -> None:
    # Recognition said "top" (a cotton shirt); the user says these are shoes.
    user_id = uuid.uuid4()
    app = build_import_confirm_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        task_id = await create_task(client, user_id)
        response = await client.post(
            f"/v1/import-tasks/{task_id}/confirm",
            headers={"X-User-Id": str(user_id)},
            json={"title": "Loafers", "category": "shoes", "color_main": "brown"},
        )

    item = await db_session.get(WardrobeItem, uuid.UUID(response.json()["data"]["item_id"]))
    assert item is not None
    assert item.formality_score is None
    assert item.season_tags is None
    assert item.material is None
    assert item.style_tags is None


async def test_cross_route_confirmation_keeps_remaining_and_never_revives_deleted_item(
    db_session: AsyncSession,
) -> None:
    user_id = uuid.uuid4()
    task = await seed_multi_candidate_task(db_session, user_id)
    first, second = task.candidate_payload["candidate_items"]
    task.candidate_payload = {"candidate_items": [{k: v for k, v in first.items() if k != "temp_item_id"}, second]}
    await db_session.flush()

    app = build_import_confirm_app(db_session)
    headers = {"X-User-Id": str(user_id)}
    prefix = f"/v1/import-tasks/{task.id}"
    title = {"title": "White t-shirt", "category": "top", "color_main": "white"}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        pending = (await client.get(prefix, headers=headers)).json()["data"]
        first_id = pending["candidate_item"]["temp_item_id"]
        assert first_id == uuid.uuid5(task.id, "0").hex

        single = await client.post(f"{prefix}/confirm", headers=headers, json=title)
        item_id = single.json()["data"]["item_id"]
        batch = await client.post(
            f"{prefix}/confirm-items", headers=headers,
            json={"items": [{**title, "temp_item_id": first_id}]},
        )
        assert batch.json()["data"] == {
            "created_items": [{"temp_item_id": first_id, "item_id": item_id}], "task_status": "waiting_confirm",
        }
        item = await db_session.get(WardrobeItem, uuid.UUID(item_id))
        assert item.name_source == "user"
        from datetime import UTC, datetime

        item.deleted_at = datetime.now(UTC)
        await db_session.flush()
        replay = await client.post(f"{prefix}/confirm", headers=headers, json=title)
        assert replay.status_code == 409

    rows = await db_session.scalars(select(WardrobeItem).where(WardrobeItem.user_id == user_id))
    assert [str(item.id) for item in rows] == [item_id]
