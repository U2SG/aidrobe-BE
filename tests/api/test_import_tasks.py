import asyncio
import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import ImportStatus
from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.import_task import ImportTask
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.session import get_db_session
from app.infrastructure.workers.import_worker import IMPORT_JOB_TYPE
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
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
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


async def test_create_import_task_returns_queued_response(db_session: AsyncSession) -> None:
    app = build_import_tasks_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            "/v1/import-tasks",
            headers={"X-User-Id": str(uuid.uuid4())},
            json={
                "input_type": "image",
                "source_type": "camera",
                "image_url": "https://cdn/shirt.jpg",
            },
        )

    assert response.status_code == 202
    data = response.json()["data"]
    assert data["status"] == "queued"
    assert data["candidate_item"] is None


async def wait_for_task_status(
    client: AsyncClient,
    task_id: str,
    user_id: uuid.UUID,
    expected_status: str,
) -> dict[str, object]:
    data: dict[str, object] = {}
    for _ in range(5):
        response = await client.get(
            f"/v1/import-tasks/{task_id}",
            headers={"X-User-Id": str(user_id)},
        )
        data = response.json()["data"]
        if data["status"] == expected_status:
            return data
        await asyncio.sleep(0)
    return data


async def test_get_import_task_returns_candidate_item(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    app = build_import_tasks_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
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

        data = await wait_for_task_status(client, task_id, user_id, "waiting_confirm")

    candidate = data["candidate_item"]
    # BE#30 — default locale is zh-CN, so the display title is Simplified
    # Chinese while category / color_main stay stable English enum fields.
    assert candidate["title"] == "黑色长裤"
    assert candidate["display_name"] == "黑色长裤"
    assert candidate["canonical_name"] == "Black trousers"
    assert candidate["localized_names"]["zh-CN"] == "黑色长裤"
    assert candidate["localized_names"]["en-US"] == "Black trousers"
    assert candidate["category"] == "bottom"


async def test_confirm_import_persists_fit_intent(db_session: AsyncSession) -> None:
    # BE#121 — the fit chips the confirm form collects must land on the item;
    # they were previously dropped (only fit_status ever reached the row).
    from sqlalchemy import select

    from app.infrastructure.persistence.models.wardrobe import WardrobeItem

    user_id = uuid.uuid4()
    app = build_import_tasks_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
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
        await wait_for_task_status(client, task_id, user_id, "waiting_confirm")
        confirm = await client.post(
            f"/v1/import-tasks/{task_id}/confirm",
            headers={"X-User-Id": str(user_id)},
            json={
                "title": "黑色长裤",
                "category": "bottom",
                "color_main": "black",
                "fit_intent": "wide_leg",
            },
        )
        assert confirm.status_code == 200
        bad = await client.post(
            f"/v1/import-tasks/{task_id}/confirm",
            headers={"X-User-Id": str(user_id)},
            json={
                "title": "黑色长裤",
                "category": "bottom",
                "color_main": "black",
                "fit_intent": "banana",
            },
        )
        assert bad.status_code == 422  # enum-typed field rejects junk values

    item = await db_session.scalar(select(WardrobeItem).where(WardrobeItem.user_id == user_id))
    assert item is not None
    assert item.fit_intent == "wide_leg"


async def test_import_candidate_respects_request_locale_en(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    app = build_import_tasks_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        create_response = await client.post(
            "/v1/import-tasks",
            headers={"X-User-Id": str(user_id)},
            json={
                "input_type": "image",
                "source_type": "album",
                "image_url": "https://cdn/pants.jpg",
                "locale": "en-US",
            },
        )
        task_id = create_response.json()["data"]["task_id"]
        data = await wait_for_task_status(client, task_id, user_id, "waiting_confirm")

    candidate = data["candidate_item"]
    # en-US request locale -> English display name; enum fields stay English.
    assert candidate["title"] == "Black trousers"
    assert candidate["display_name"] == "Black trousers"
    assert candidate["canonical_name"] == "Black trousers"
    assert candidate["category"] == "bottom"
    assert candidate["color_main"] == "black"


async def test_create_import_task_async_path_enqueues_job(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # BE#26 — the flag is read in-body via get_settings() (lru_cached, bypasses
    # dependency_overrides), so monkeypatch the consuming module. With it on,
    # create enqueues an import_task job instead of running BackgroundTasks; the
    # task stays queued (a drainer, not this request, will process it).
    monkeypatch.setattr(
        import_tasks_router,
        "get_settings",
        lambda: Settings(APP_ENV="dev", VISION_PROVIDER="fake", ENABLE_IMPORT_ASYNC_JOBS=True),
    )
    user_id = uuid.uuid4()
    app = build_import_tasks_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            "/v1/import-tasks",
            headers={"X-User-Id": str(user_id)},
            json={"input_type": "image", "source_type": "camera", "image_url": "https://cdn/shirt.jpg"},
        )

    assert response.status_code == 202
    task_id = response.json()["data"]["task_id"]
    job = (
        await db_session.execute(select(AsyncJob).where(AsyncJob.job_type == IMPORT_JOB_TYPE))
    ).scalar_one()
    assert str(job.target_id) == task_id
    assert job.status == "queued"
    assert job.idempotency_key == f"import_task:{task_id}"
    task = (
        await db_session.execute(select(ImportTask).where(ImportTask.id == uuid.UUID(task_id)))
    ).scalar_one()
    assert task.task_status == ImportStatus.QUEUED.value  # not processed inline


async def test_failed_vision_response_stores_failed_task(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    app = build_import_tasks_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            "/v1/import-tasks",
            headers={"X-User-Id": str(user_id)},
            json={
                "input_type": "image",
                "source_type": "camera",
                "image_url": "https://cdn/fail.jpg",
            },
        )

    assert response.status_code == 202
    task_id = response.json()["data"]["task_id"]
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        data = await wait_for_task_status(client, task_id, user_id, "failed")
    assert data["status"] == "failed"
    assert data["error_code"] == "vision_failed"


async def test_missing_or_other_user_task_returns_404(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    other_user_id = uuid.uuid4()
    app = build_import_tasks_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
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


async def _seed_task(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    status: str,
    candidate_payload: dict[str, object] | None,
) -> uuid.UUID:
    session.add(User(id=user_id))
    await session.flush()
    task = ImportTask(
        user_id=user_id,
        input_type="image",
        source_type="album",
        task_status=status,
        input_payload={"image_url": "https://cdn/x.jpg", "source_url": None, "locale": None},
        candidate_payload=candidate_payload,
    )
    session.add(task)
    await session.flush()
    return task.id


async def test_legacy_single_payload_maps_to_one_candidate_item(db_session: AsyncSession) -> None:
    # BE#24 — an old candidate_item-only payload (no nested candidate_items)
    # maps to candidate_items length 1; candidate_item mirrors it.
    user_id = uuid.uuid4()
    task_id = await _seed_task(
        db_session,
        user_id,
        status=ImportStatus.WAITING_CONFIRM.value,
        candidate_payload={
            "title": "Black trousers",
            "category": "bottom",
            "color_main": "black",
            "confidence_score": 0.85,
            "original_image_url": "https://cdn/pants.jpg",
        },
    )
    app = build_import_tasks_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.get(
            f"/v1/import-tasks/{task_id}", headers={"X-User-Id": str(user_id)}
        )

    data = response.json()["data"]
    assert len(data["candidate_items"]) == 1
    assert data["candidate_items"][0]["title"] == "Black trousers"
    assert data["candidate_items"][0]["temp_item_id"] is None
    assert data["candidate_item"]["title"] == "Black trousers"


async def test_multi_candidate_payload_returns_both_fields(db_session: AsyncSession) -> None:
    # BE#24 — a multi-candidate payload returns the full list AND a compat
    # candidate_item set to the first should_import=True candidate.
    user_id = uuid.uuid4()
    task_id = await _seed_task(
        db_session,
        user_id,
        status=ImportStatus.WAITING_CONFIRM.value,
        candidate_payload={
            # Top-level single shape kept for confirm_import compat.
            "title": "White shirt",
            "category": "top",
            "color_main": "white",
            "confidence_score": 0.9,
            "original_image_url": "https://cdn/look.jpg",
            "candidate_items": [
                {
                    "temp_item_id": "cand-a",
                    "title": "Black trousers",
                    "category": "bottom",
                    "color_main": "black",
                    "confidence_score": 0.85,
                    "original_image_url": "https://cdn/look.jpg",
                    "should_import": False,
                },
                {
                    "temp_item_id": "cand-b",
                    "title": "White shirt",
                    "category": "top",
                    "color_main": "white",
                    "confidence_score": 0.9,
                    "original_image_url": "https://cdn/look.jpg",
                    "should_import": True,
                },
            ],
        },
    )
    app = build_import_tasks_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.get(
            f"/v1/import-tasks/{task_id}", headers={"X-User-Id": str(user_id)}
        )

    data = response.json()["data"]
    assert len(data["candidate_items"]) == 2
    assert {c["temp_item_id"] for c in data["candidate_items"]} == {"cand-a", "cand-b"}
    # first recommended (should_import) candidate is the compat candidate_item.
    assert data["candidate_item"]["temp_item_id"] == "cand-b"
    assert data["candidate_item"]["title"] == "White shirt"


async def test_processing_and_failed_return_empty_candidate_items(db_session: AsyncSession) -> None:
    # BE#24 — in-flight / failed tasks have no candidate payload yet; the list
    # must be a safe empty array (not null) and candidate_item null.
    processing_user = uuid.uuid4()
    processing_task = await _seed_task(
        db_session,
        processing_user,
        status=ImportStatus.PROCESSING.value,
        candidate_payload=None,
    )
    app = build_import_tasks_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.get(
            f"/v1/import-tasks/{processing_task}",
            headers={"X-User-Id": str(processing_user)},
        )

    data = response.json()["data"]
    assert data["status"] == "processing"
    assert data["candidate_items"] == []
    assert data["candidate_item"] is None


async def test_vision_worker_writes_length_one_candidate_items(db_session: AsyncSession) -> None:
    # BE#24 — the live vision flow yields one item: candidate_items length 1,
    # each carries a stable temp_item_id, and candidate_item mirrors the first.
    user_id = uuid.uuid4()
    app = build_import_tasks_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
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
        data = await wait_for_task_status(client, task_id, user_id, "waiting_confirm")

    candidate_items = data["candidate_items"]
    assert len(candidate_items) == 1
    assert candidate_items[0]["temp_item_id"]
    assert candidate_items[0]["should_import"] is True
    assert data["candidate_item"]["title"] == candidate_items[0]["title"]


async def test_outfit_vision_worker_writes_each_detected_garment(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    app = build_import_tasks_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        create_response = await client.post(
            "/v1/import-tasks",
            headers={"X-User-Id": str(user_id)},
            json={
                "input_type": "outfit_image",
                "source_type": "album",
                "image_url": "https://cdn/outfit.jpg",
            },
        )
        assert create_response.status_code == 202
        task_id = create_response.json()["data"]["task_id"]
        data = await wait_for_task_status(client, task_id, user_id, "waiting_confirm")

    candidate_items = data["candidate_items"]
    assert len(candidate_items) == 2
    assert [item["category"] for item in candidate_items] == ["top", "bottom"]
    assert all(item["temp_item_id"] for item in candidate_items)
    assert len({item["temp_item_id"] for item in candidate_items}) == 2
    assert all(item["should_import"] is True for item in candidate_items)
    assert data["candidate_item"]["title"] == candidate_items[0]["title"]
