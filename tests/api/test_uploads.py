from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.presentation.api.v1.routers import uploads as uploads_router
from app.presentation.middlewares.exception_handler import setup_exception_handlers


def build_uploads_app() -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)
    app.include_router(uploads_router.router)
    return app


async def test_presign_accepts_image_content_type() -> None:
    app = build_uploads_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/v1/uploads/presign",
            json={
                "file_name": "shirt.jpg",
                "content_type": "image/jpeg",
                "biz_type": "wardrobe_item",
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["code"] == 0
    assert set(body["data"]) == {"upload_url", "file_url", "object_key", "fields"}
    assert body["data"]["object_key"].endswith("/shirt.jpg")


async def test_presign_rejects_non_image_content_type() -> None:
    app = build_uploads_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/v1/uploads/presign",
            json={"file_name": "note.txt", "content_type": "text/plain"},
        )

    assert response.status_code == 422
    assert response.json()["code"] == 4001


async def test_presign_enforces_max_content_length() -> None:
    app = build_uploads_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        response = await client.post(
            "/v1/uploads/presign",
            json={
                "file_name": "shirt.jpg",
                "content_type": "image/jpeg",
                "max_content_length": 10 * 1024 * 1024 + 1,
            },
        )

    assert response.status_code == 422
    assert response.json()["code"] == 4001
