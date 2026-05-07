"""C4 — admin async-jobs endpoint."""

from __future__ import annotations

import uuid

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.session import get_db_session
from app.infrastructure.workers.job_runtime import enqueue
from app.presentation.api.v1.routers import admin as admin_router
from app.presentation.middlewares.exception_handler import setup_exception_handlers


def _build_app(session: AsyncSession, settings_override: Settings) -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)

    async def override_db() -> AsyncSession:
        return session

    def override_settings() -> Settings:
        return settings_override

    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[get_settings] = override_settings
    app.include_router(admin_router.router)
    return app


async def _seed_dead_job(session: AsyncSession, *, job_type: str, user_id: uuid.UUID | None = None) -> uuid.UUID:
    job, _ = await enqueue(
        session,
        job_type=job_type,
        idempotency_key=f"{job_type}:{uuid.uuid4()}",
        user_id=user_id,
    )
    job.status = "dead"
    job.last_error = "boom"
    job.attempts = 3
    await session.flush()
    return job.id


async def test_admin_jobs_lists_dead_by_default(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    db_session.add(User(id=user_id))
    await db_session.flush()

    dead_id = await _seed_dead_job(db_session, job_type="embedding", user_id=user_id)
    # A succeeded job that should NOT appear in the default `dead` filter.
    await enqueue(
        db_session,
        job_type="embedding",
        idempotency_key=f"alive:{uuid.uuid4()}",
        user_id=user_id,
    )

    app = _build_app(db_session, Settings(APP_ENV="dev"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get("/v1/admin/jobs", headers={"X-User-Id": str(user_id)})

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert len(body["items"]) == 1
    assert body["items"][0]["job_id"] == str(dead_id)
    assert body["items"][0]["status"] == "dead"
    assert body["items"][0]["last_error"] == "boom"


async def test_admin_jobs_filters_by_job_type(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    db_session.add(User(id=user_id))
    await db_session.flush()

    embedding_dead = await _seed_dead_job(db_session, job_type="embedding", user_id=user_id)
    await _seed_dead_job(db_session, job_type="outfit_board", user_id=user_id)

    app = _build_app(db_session, Settings(APP_ENV="dev"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get(
            "/v1/admin/jobs?job_type=embedding",
            headers={"X-User-Id": str(user_id)},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["job_id"] == str(embedding_dead)


async def test_admin_jobs_unknown_status_falls_back_to_dead(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    db_session.add(User(id=user_id))
    await _seed_dead_job(db_session, job_type="embedding", user_id=user_id)

    app = _build_app(db_session, Settings(APP_ENV="dev"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get(
            "/v1/admin/jobs?status=garbage",
            headers={"X-User-Id": str(user_id)},
        )

    assert resp.status_code == 200
    body = resp.json()
    assert body["total"] == 1
    assert body["items"][0]["status"] == "dead"


async def test_admin_jobs_pagination(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    db_session.add(User(id=user_id))
    await db_session.flush()

    for _ in range(5):
        await _seed_dead_job(db_session, job_type="embedding", user_id=user_id)

    app = _build_app(db_session, Settings(APP_ENV="dev"))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        page1 = await client.get(
            "/v1/admin/jobs?limit=2&offset=0",
            headers={"X-User-Id": str(user_id)},
        )
        page2 = await client.get(
            "/v1/admin/jobs?limit=2&offset=2",
            headers={"X-User-Id": str(user_id)},
        )

    assert page1.status_code == 200
    assert page2.status_code == 200
    p1 = page1.json()
    p2 = page2.json()
    assert p1["total"] == 5
    assert p2["total"] == 5
    assert len(p1["items"]) == 2
    assert len(p2["items"]) == 2
    p1_ids = {item["job_id"] for item in p1["items"]}
    p2_ids = {item["job_id"] for item in p2["items"]}
    assert p1_ids.isdisjoint(p2_ids)


async def test_admin_jobs_prod_without_admin_ids_returns_403(db_session: AsyncSession) -> None:
    """Empty ADMIN_USER_IDS in prod must fail closed — admin surface is
    locked off until the operator opts in. Otherwise any authenticated
    user could browse dead jobs."""
    user_id = uuid.uuid4()
    db_session.add(User(id=user_id))
    await db_session.flush()

    # Prod settings with no admin allowlist.
    prod_settings = Settings(
        APP_ENV="prod",
        ADMIN_USER_IDS="",
        AUTH_APPLE_VERIFY_SIGNATURE=False,
    )
    app = _build_app(db_session, prod_settings)

    # In prod the X-User-Id shortcut is rejected, so we mock past auth by
    # overriding require_admin_user's upstream get_current_user. The point
    # of this test is the admin gate behavior, not the user resolution path.
    from app.infrastructure.persistence.repositories.user_repo import (
        SqlAlchemyUserRepository,
    )

    repo = SqlAlchemyUserRepository(db_session)
    user = await repo.get_or_create(user_id)
    from app.presentation.api.v1.deps import get_current_user

    async def fake_user() -> User:
        return user

    app.dependency_overrides[get_current_user] = fake_user

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get("/v1/admin/jobs")

    assert resp.status_code == 403


async def test_admin_jobs_prod_allows_admin_in_allowlist(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    db_session.add(User(id=user_id))
    await db_session.flush()
    await _seed_dead_job(db_session, job_type="embedding", user_id=user_id)

    prod_settings = Settings(
        APP_ENV="prod",
        ADMIN_USER_IDS=str(user_id),
        AUTH_APPLE_VERIFY_SIGNATURE=False,
    )
    app = _build_app(db_session, prod_settings)

    from app.infrastructure.persistence.repositories.user_repo import (
        SqlAlchemyUserRepository,
    )

    repo = SqlAlchemyUserRepository(db_session)
    user = await repo.get_or_create(user_id)
    from app.presentation.api.v1.deps import get_current_user

    async def fake_user() -> User:
        return user

    app.dependency_overrides[get_current_user] = fake_user

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get("/v1/admin/jobs")

    assert resp.status_code == 200
    assert resp.json()["total"] == 1


async def test_admin_jobs_prod_rejects_non_admin_user(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    other_admin_id = uuid.uuid4()
    db_session.add(User(id=user_id))
    await db_session.flush()

    prod_settings = Settings(
        APP_ENV="prod",
        ADMIN_USER_IDS=str(other_admin_id),  # someone else, not user_id
        AUTH_APPLE_VERIFY_SIGNATURE=False,
    )
    app = _build_app(db_session, prod_settings)

    from app.infrastructure.persistence.repositories.user_repo import (
        SqlAlchemyUserRepository,
    )

    repo = SqlAlchemyUserRepository(db_session)
    user = await repo.get_or_create(user_id)
    from app.presentation.api.v1.deps import get_current_user

    async def fake_user() -> User:
        return user

    app.dependency_overrides[get_current_user] = fake_user

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get("/v1/admin/jobs")

    assert resp.status_code == 403
