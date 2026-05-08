"""Phase E1 — DELETE /v1/me + auth gate + scheduled hard purge."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.audit_log import AuditLog
from app.infrastructure.persistence.models.user import User, UserSession
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import account as account_router
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
    app.include_router(account_router.router)
    return app


async def _seed_user(session: AsyncSession) -> uuid.UUID:
    user_id = uuid.uuid4()
    session.add(User(id=user_id))
    await session.flush()
    return user_id


async def test_delete_me_returns_503_when_flag_off(db_session: AsyncSession) -> None:
    user_id = await _seed_user(db_session)
    app = _build_app(db_session, Settings(APP_ENV="dev", ENABLE_USER_PURGE=False))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.delete("/v1/me", headers={"X-User-Id": str(user_id)})
    assert resp.status_code == 503


async def test_delete_me_soft_deletes_and_schedules_purge(db_session: AsyncSession) -> None:
    user_id = await _seed_user(db_session)
    # Add a live session to confirm revocation.
    db_session.add(
        UserSession(
            user_id=user_id,
            refresh_token_hash=uuid.uuid4().hex,
            expires_at=datetime.now(UTC) + timedelta(days=1),
        )
    )
    await db_session.flush()

    app = _build_app(
        db_session,
        Settings(APP_ENV="dev", ENABLE_USER_PURGE=True, USER_PURGE_GRACE_DAYS=30),
    )
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.delete("/v1/me", headers={"X-User-Id": str(user_id)})
    assert resp.status_code == 200, resp.text
    body = resp.json()["data"]
    assert body["grace_days"] == 30

    # User row stamped.
    user = await db_session.get(User, user_id)
    assert user is not None
    assert user.deactivated_at is not None
    assert user.purged_at is None

    # All sessions revoked.
    revoked = (
        await db_session.execute(
            select(UserSession).where(UserSession.user_id == user_id)
        )
    ).scalars().all()
    assert all(s.revoked_at is not None for s in revoked)

    # async_jobs row enqueued at the grace boundary.
    job = (
        await db_session.execute(
            select(AsyncJob).where(
                AsyncJob.idempotency_key == f"user_purge:{user_id}"
            )
        )
    ).scalar_one()
    assert job.status == "queued"
    assert job.next_run_at > datetime.now(UTC) + timedelta(days=29)
    assert job.next_run_at < datetime.now(UTC) + timedelta(days=31)
    assert job.target_id == user_id

    # audit_log entry written.
    audit = (
        await db_session.execute(
            select(AuditLog).where(
                AuditLog.user_id == user_id, AuditLog.action == "user.deactivate"
            )
        )
    ).scalar_one()
    assert audit.actor == "user"


async def test_delete_me_idempotent_on_already_deactivated(db_session: AsyncSession) -> None:
    user_id = await _seed_user(db_session)
    user = await db_session.get(User, user_id)
    assert user is not None
    # First deactivation: stamps deactivated_at AND enqueues the purge job.
    # We exercise the use case directly because `get_current_user` blocks
    # deactivated users, so DELETE /v1/me would 401 on the second call.
    from app.application.users.deactivate_account import deactivate_account

    first = await deactivate_account(db_session, user)
    await db_session.commit()
    first_deactivated_at = first.deactivated_at
    assert first_deactivated_at is not None

    # Second call must be a no-op: timestamp preserved, purge job not
    # re-enqueued (idempotency_key short-circuits).
    refreshed = await deactivate_account(db_session, user)
    await db_session.commit()
    assert refreshed.deactivated_at == first_deactivated_at

    # Exactly one user_purge job exists across both calls.
    rows = (
        await db_session.execute(
            select(AsyncJob).where(
                AsyncJob.idempotency_key == f"user_purge:{user_id}"
            )
        )
    ).scalars().all()
    assert len(rows) == 1


async def test_deactivated_user_blocked_by_auth_gate(db_session: AsyncSession) -> None:
    """A user who deactivated must not be able to authenticate on the next
    request — JWTs from before deactivation become useless via the auth
    gate, not by waiting for natural expiry."""
    user_id = await _seed_user(db_session)
    user = await db_session.get(User, user_id)
    assert user is not None
    user.deactivated_at = datetime.now(UTC)
    await db_session.commit()

    app = _build_app(db_session, Settings(APP_ENV="dev", ENABLE_USER_PURGE=True))
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.delete("/v1/me", headers={"X-User-Id": str(user_id)})
    assert resp.status_code == 401
