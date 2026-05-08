"""Phase E5 — POST /v1/uploads/sts-token via fake provider."""

from __future__ import annotations

import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import uploads as uploads_router
from app.presentation.middlewares.exception_handler import setup_exception_handlers


def _build_app(session: AsyncSession, settings_override: Settings) -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)

    async def override_db_session() -> AsyncSession:
        return session

    def override_settings() -> Settings:
        return settings_override

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = override_settings
    app.include_router(uploads_router.router)
    return app


async def _seed_user(session: AsyncSession) -> uuid.UUID:
    user_id = uuid.uuid4()
    session.add(User(id=user_id))
    await session.flush()
    return user_id


async def test_sts_endpoint_503_when_flag_off(db_session: AsyncSession) -> None:
    user_id = await _seed_user(db_session)
    app = _build_app(
        db_session,
        Settings(APP_ENV="dev", ENABLE_OSS_STS=False, ALIYUN_OSS_BUCKET="aidrobe-items"),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.post(
            "/v1/uploads/sts-token",
            json={"bucket_alias": "items", "action": "upload"},
            headers={"X-User-Id": str(user_id)},
        )
    assert resp.status_code == 503


async def test_sts_endpoint_returns_token_scoped_to_user(
    db_session: AsyncSession,
) -> None:
    """The fake provider deterministically encodes user_id in the prefix.
    This test asserts the endpoint actually issues per-user-scoped tokens
    rather than reusing one global token."""
    user_id = await _seed_user(db_session)
    app = _build_app(
        db_session,
        Settings(APP_ENV="dev", ENABLE_OSS_STS=True, ALIYUN_OSS_BUCKET="aidrobe-items"),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.post(
            "/v1/uploads/sts-token",
            json={"bucket_alias": "items", "action": "upload"},
            headers={"X-User-Id": str(user_id)},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["bucket"] == "aidrobe-items"
    assert body["object_key_prefix"] == f"users/{user_id}/upload/"
    assert body["access_key_id"].startswith("FAKE-AK-")
    assert body["security_token"].endswith("-upload")


async def test_sts_endpoint_503_when_bucket_not_configured(
    db_session: AsyncSession,
) -> None:
    """Operator hasn't filled in the bucket name yet — fail closed rather
    than issuing creds against an empty target."""
    user_id = await _seed_user(db_session)
    app = _build_app(
        db_session, Settings(APP_ENV="dev", ENABLE_OSS_STS=True, ALIYUN_OSS_BUCKET="")
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.post(
            "/v1/uploads/sts-token",
            json={"bucket_alias": "items", "action": "upload"},
            headers={"X-User-Id": str(user_id)},
        )
    assert resp.status_code == 503


async def test_sts_endpoint_validates_action(db_session: AsyncSession) -> None:
    user_id = await _seed_user(db_session)
    app = _build_app(
        db_session,
        Settings(APP_ENV="dev", ENABLE_OSS_STS=True, ALIYUN_OSS_BUCKET="aidrobe-items"),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.post(
            "/v1/uploads/sts-token",
            json={"bucket_alias": "items", "action": "delete"},
            headers={"X-User-Id": str(user_id)},
        )
    # Pydantic Literal rejects "delete".
    assert resp.status_code == 422
