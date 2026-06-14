"""OOTD poster API tests — flag gating, consent requirement, happy path.

Uses a self-built app with get_db_session overridden to the testcontainers
session (the shared async_client fixture talks to the real .env DB), mirroring
tests/api/test_today_context.py.
"""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings
from app.infrastructure.cache.client import get_redis_client
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import ootd_posters, outfit_visual
from app.presentation.middlewares.exception_handler import setup_exception_handlers


def _build_app(session: AsyncSession) -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)

    async def _db() -> AsyncSession:
        return session

    app.dependency_overrides[get_db_session] = _db
    app.dependency_overrides[get_redis_client] = lambda: None  # cost guard off → unused
    app.include_router(ootd_posters.router)
    app.include_router(outfit_visual.router)
    return app


def _enable_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    # Router calls get_settings() directly (lru-cached, bypasses dep override).
    monkeypatch.setattr(
        ootd_posters,
        "get_settings",
        lambda: Settings(APP_ENV="dev", ENABLE_OOTD_POSTER=True),
    )


async def _post(app: FastAPI, headers: dict[str, str], json: dict[str, object]):
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        return await client.post("/v1/ootd-posters", headers=headers, json=json)


async def test_flag_off_returns_503(db_session: AsyncSession) -> None:
    app = _build_app(db_session)
    resp = await _post(
        app,
        {"X-User-Id": str(uuid.uuid4())},
        {"ootd_image_url": "https://x/selfie.jpg", "consent": True},
    )
    assert resp.status_code == 503


async def test_consent_required_returns_403(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_flag(monkeypatch)
    app = _build_app(db_session)
    resp = await _post(
        app,
        {"X-User-Id": str(uuid.uuid4())},
        {"ootd_image_url": "https://x/selfie.jpg", "consent": False},
    )
    assert resp.status_code == 403


async def test_missing_source_returns_422(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_flag(monkeypatch)
    app = _build_app(db_session)
    resp = await _post(app, {"X-User-Id": str(uuid.uuid4())}, {"consent": True})
    assert resp.status_code == 422


async def test_happy_path_creates_queued_asset(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_flag(monkeypatch)
    app = _build_app(db_session)
    user_id = str(uuid.uuid4())
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/ootd-posters",
            headers={"X-User-Id": user_id},
            json={
                "ootd_image_url": "https://x/selfie.jpg",
                "template_id": "studio",
                "consent": True,
            },
        )
        assert resp.status_code == 202, resp.text
        body = resp.json()
        assert body["generation_status"] == "queued"
        assert body["disclaimer"]
        asset_id = body["asset_id"]

        poll = await client.get(
            f"/v1/outfit-visual-assets/{asset_id}", headers={"X-User-Id": user_id}
        )
        assert poll.status_code == 200
        poll_body = poll.json()
        assert poll_body["generation_status"] == "queued"
        assert poll_body["visual_type"] == "ootd_poster"
