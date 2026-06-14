"""M19 — POST /v1/style/sources (PDF magazine ingest) API tests.

Cover:
- flag on: a pdf_magazine source persists the parent row, enqueues a pdf_ingest
  async job, and returns {source_id, status: queued, estimated_pages}.
- flag off: 503 kill switch.
- input_type validation (only pdf_magazine accepted).
"""

import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.style import UserStyleProfile
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import style as style_router
from app.presentation.middlewares.exception_handler import setup_exception_handlers


def build_style_app(session: AsyncSession, *, enable_pdf: bool) -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)

    async def override_db_session() -> AsyncSession:
        return session

    def override_settings() -> Settings:
        return Settings(APP_ENV="dev", ENABLE_PDF_STYLE_INGEST=enable_pdf)

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = override_settings
    app.include_router(style_router.router)
    return app


async def test_pdf_source_enqueues_job_when_enabled(db_session: AsyncSession) -> None:
    app = build_style_app(db_session, enable_pdf=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            "/v1/style/sources",
            headers={"X-User-Id": str(uuid.uuid4())},
            json={
                "file_url": "https://oss.example.com/elle-2026.pdf",
                "input_type": "pdf_magazine",
                "title": "ELLE 2026",
            },
        )
    assert response.status_code == 201
    data = response.json()["data"]
    assert data["status"] == "queued"
    assert data["estimated_pages"] == 0
    source_id = uuid.UUID(data["source_id"])

    # Parent row persisted as pdf_magazine.
    parent = await db_session.get(UserStyleProfile, source_id)
    assert parent is not None
    assert parent.source_type == "pdf_magazine"
    assert parent.source_url == "https://oss.example.com/elle-2026.pdf"
    assert parent.learn_status == "queued"

    # pdf_ingest async job enqueued for this parent.
    job_count = await db_session.scalar(
        select(func.count())
        .select_from(AsyncJob)
        .where(AsyncJob.job_type == "pdf_ingest", AsyncJob.target_id == source_id)
    )
    assert job_count == 1


async def test_pdf_source_returns_503_when_disabled(db_session: AsyncSession) -> None:
    app = build_style_app(db_session, enable_pdf=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            "/v1/style/sources",
            headers={"X-User-Id": str(uuid.uuid4())},
            json={"file_url": "https://oss.example.com/elle-2026.pdf", "input_type": "pdf_magazine"},
        )
    assert response.status_code == 503

    # Nothing persisted on the kill-switch path.
    count = await db_session.scalar(
        select(func.count()).select_from(UserStyleProfile).where(UserStyleProfile.source_type == "pdf_magazine")
    )
    assert count == 0


async def test_pdf_source_rejects_unknown_input_type(db_session: AsyncSession) -> None:
    app = build_style_app(db_session, enable_pdf=True)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            "/v1/style/sources",
            headers={"X-User-Id": str(uuid.uuid4())},
            json={"file_url": "https://oss.example.com/x.pdf", "input_type": "image_batch"},
        )
    assert response.status_code == 422
    assert response.json()["code"] == 4001
