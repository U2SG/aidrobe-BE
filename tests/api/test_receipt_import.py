import asyncio
import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.import_task import ImportTask
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import import_tasks as import_tasks_router
from app.presentation.middlewares.exception_handler import setup_exception_handlers


def build_app(
    session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    *,
    enable_receipt: bool = True,
) -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)

    # OCR_PROVIDER="fake" so tests run without a real API key.
    settings = Settings(
        APP_ENV="dev",
        VISION_PROVIDER="fake",
        OCR_PROVIDER="fake",
        ENABLE_RECEIPT_IMPORT=enable_receipt,
    )

    async def override_db_session() -> AsyncSession:
        return session

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = lambda: settings
    # The router calls get_settings() directly (lru_cached), which bypasses the
    # dependency override — patch the consuming module too, or the flag has no
    # effect in a clean env. See CLAUDE.md "Tests that flip a feature flag".
    monkeypatch.setattr(import_tasks_router, "get_settings", lambda: settings)
    app.include_router(import_tasks_router.router)
    return app


async def _wait_for_status(
    client: AsyncClient,
    task_id: str,
    user_id: uuid.UUID,
    expected: str,
) -> dict[str, object]:
    data: dict[str, object] = {}
    for _ in range(5):
        response = await client.get(
            f"/v1/import-tasks/{task_id}",
            headers={"X-User-Id": str(user_id)},
        )
        data = response.json()["data"]
        if data["status"] == expected:
            return data
        await asyncio.sleep(0)
    return data


async def _create(
    client: AsyncClient,
    user_id: uuid.UUID,
    input_type: str,
    image_url: str,
) -> str:
    response = await client.post(
        "/v1/import-tasks",
        headers={"X-User-Id": str(user_id)},
        json={
            "input_type": input_type,
            "source_type": "screenshot",
            "image_url": image_url,
        },
    )
    assert response.status_code == 202, response.text
    return str(response.json()["data"]["task_id"])


async def test_receipt_screenshot_produces_ocr_candidate(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = uuid.uuid4()
    app = build_app(db_session, monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        task_id = await _create(client, user_id, "receipt_screenshot", "https://cdn/receipt-shirt.jpg")
        data = await _wait_for_status(client, task_id, user_id, "waiting_confirm")

    candidate = data["candidate_item"]
    assert candidate is not None
    assert candidate["title"] == "MUJI Oxford Shirt"
    assert candidate["category"] == "top"
    assert candidate["color_main"] == "white"
    assert candidate["size_label"] == "L"
    assert candidate["purchase_price"] == "299.00"
    assert candidate["purchase_date"] == "2026-02-20"


async def test_order_screenshot_produces_ocr_candidate(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = uuid.uuid4()
    app = build_app(db_session, monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        task_id = await _create(client, user_id, "order_screenshot", "https://cdn/order-pants.jpg")
        data = await _wait_for_status(client, task_id, user_id, "waiting_confirm")

    candidate = data["candidate_item"]
    assert candidate is not None
    assert candidate["category"] == "bottom"
    assert candidate["size_label"] == "M"
    assert candidate["purchase_price"] == "199.00"


async def test_shopping_email_produces_ocr_candidate(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = uuid.uuid4()
    app = build_app(db_session, monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        task_id = await _create(client, user_id, "shopping_email", "https://cdn/email-shirt.jpg")
        data = await _wait_for_status(client, task_id, user_id, "waiting_confirm")

    candidate = data["candidate_item"]
    assert candidate is not None
    assert candidate["title"] == "MUJI Oxford Shirt"


async def test_ocr_failure_marks_task_failed(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    user_id = uuid.uuid4()
    app = build_app(db_session, monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        task_id = await _create(client, user_id, "receipt_screenshot", "https://cdn/fail.jpg")
        data = await _wait_for_status(client, task_id, user_id, "failed")

    assert data["status"] == "failed"
    assert data["error_code"] == "ocr_failed"


async def test_confirm_auto_fills_price_and_size_from_candidate(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = uuid.uuid4()
    app = build_app(db_session, monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        task_id = await _create(client, user_id, "receipt_screenshot", "https://cdn/receipt-shirt.jpg")
        await _wait_for_status(client, task_id, user_id, "waiting_confirm")
        # Client confirms without supplying price/size/date — they auto-fill
        # from the OCR candidate.
        response = await client.post(
            f"/v1/import-tasks/{task_id}/confirm",
            headers={"X-User-Id": str(user_id)},
            json={"title": "MUJI Oxford Shirt", "category": "top", "color_main": "white"},
        )

    assert response.status_code == 200, response.text
    item_id = response.json()["data"]["item_id"]
    item = await db_session.get(WardrobeItem, uuid.UUID(item_id))
    assert item is not None
    assert item.size_label == "L"
    assert str(item.purchase_price) == "299.00"
    assert item.purchase_date is not None
    assert item.purchase_date.isoformat() == "2026-02-20"

    task = await db_session.get(ImportTask, uuid.UUID(task_id))
    assert task is not None
    assert task.task_status == "completed"


async def test_confirm_request_overrides_ocr_auto_fill(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    user_id = uuid.uuid4()
    app = build_app(db_session, monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        task_id = await _create(client, user_id, "receipt_screenshot", "https://cdn/receipt-shirt.jpg")
        await _wait_for_status(client, task_id, user_id, "waiting_confirm")
        response = await client.post(
            f"/v1/import-tasks/{task_id}/confirm",
            headers={"X-User-Id": str(user_id)},
            json={
                "title": "MUJI Oxford Shirt",
                "category": "top",
                "color_main": "white",
                "size_label": "XL",
                "purchase_price": "350.00",
                "purchase_date": "2026-03-01",
            },
        )

    assert response.status_code == 200, response.text
    item_id = response.json()["data"]["item_id"]
    item = await db_session.get(WardrobeItem, uuid.UUID(item_id))
    assert item is not None
    assert item.size_label == "XL"
    assert str(item.purchase_price) == "350.00"
    assert item.purchase_date is not None
    assert item.purchase_date.isoformat() == "2026-03-01"


async def test_receipt_import_disabled_returns_400(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """When ENABLE_RECEIPT_IMPORT=False, receipt input_types are rejected."""
    user_id = uuid.uuid4()
    app = build_app(db_session, monkeypatch, enable_receipt=False)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            "/v1/import-tasks",
            headers={"X-User-Id": str(user_id)},
            json={
                "input_type": "receipt_screenshot",
                "source_type": "screenshot",
                "image_url": "https://cdn/receipt.jpg",
            },
        )
    assert response.status_code == 400
