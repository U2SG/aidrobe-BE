"""BE#83 — profile data-trust: memory review/correct/clear + clear-all-data."""

import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import Category, ItemStatus
from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.audit_log import AuditLog
from app.infrastructure.persistence.models.preference_memory import UserPreferenceMemory
from app.infrastructure.persistence.models.user import User, UserSettings
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.preference_memory_repo import (
    SqlAlchemyPreferenceMemoryRepository,
)
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import profile
from app.presentation.middlewares.exception_handler import setup_exception_handlers


def build_profile_app(session: AsyncSession) -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)

    async def override_db_session() -> AsyncSession:
        return session

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = lambda: Settings(APP_ENV="dev")
    app.include_router(profile.router)
    return app


@pytest.fixture
def data_trust_on(monkeypatch: pytest.MonkeyPatch) -> None:
    merged = Settings(APP_ENV="dev", ENABLE_DATA_TRUST=True)
    monkeypatch.setattr(
        "app.presentation.api.v1.routers.profile.get_settings", lambda: merged
    )


async def _seed_user(session: AsyncSession) -> uuid.UUID:
    user_id = uuid.uuid4()
    session.add(User(id=user_id))
    await session.flush()
    return user_id


async def _seed_pref(
    session: AsyncSession,
    user_id: uuid.UUID,
    value: str,
    *,
    pref_type: str = "color",
    source: str = "explicit_user_statement",
    confidence: float = 0.8,
) -> UserPreferenceMemory:
    return await SqlAlchemyPreferenceMemoryRepository(session).upsert(
        user_id=user_id,
        preference_type=pref_type,
        preference_value=value,
        polarity="negative",
        confidence=confidence,
        source=source,
    )


def _client(session: AsyncSession) -> AsyncClient:
    return AsyncClient(
        transport=ASGITransport(app=build_profile_app(session)),
        base_url="http://testserver",
    )


async def test_list_memory_maps_source_and_confidence(
    db_session: AsyncSession, data_trust_on: None
) -> None:
    user_id = await _seed_user(db_session)
    await _seed_pref(db_session, user_id, "不喜欢亮色", source="explicit_user_statement", confidence=0.8)
    await _seed_pref(
        db_session, user_id, "偏好深色", pref_type="style", source="feedback_signal", confidence=0.3
    )
    async with _client(db_session) as client:
        resp = await client.get("/v1/profile/memory", headers={"X-User-Id": str(user_id)})
    assert resp.status_code == 200
    prefs = resp.json()["data"]["preferences"]
    by_label = {p["label"]: p for p in prefs}
    assert by_label["不喜欢亮色"]["source"] == "explicit"
    assert by_label["不喜欢亮色"]["confidence"] == "high"
    assert by_label["偏好深色"]["source"] == "feedback"
    assert by_label["偏好深色"]["confidence"] == "low"


async def test_body_shape_labels_replaced_with_functional_copy(
    db_session: AsyncSession, data_trust_on: None
) -> None:
    user_id = await _seed_user(db_session)
    await _seed_pref(db_session, user_id, "梨形身材要遮胯", pref_type="fit")
    await _seed_pref(db_session, user_id, "显瘦优先", pref_type="fit")
    async with _client(db_session) as client:
        resp = await client.get("/v1/profile/memory", headers={"X-User-Id": str(user_id)})
    prefs = resp.json()["data"]["preferences"]
    # Both items are still LISTED (user must be able to see + delete everything),
    # but with functional labels — never body-shape / body-correction wording.
    assert len(prefs) == 2
    for p in prefs:
        assert "梨形" not in p["label"]
        assert "显瘦" not in p["label"]
        assert "遮" not in p["label"]


async def test_delete_and_mark_wrong_remove_item_and_audit(
    db_session: AsyncSession, data_trust_on: None
) -> None:
    user_id = await _seed_user(db_session)
    row1 = await _seed_pref(db_session, user_id, "不喜欢亮色")
    row2 = await _seed_pref(db_session, user_id, "不喜欢碎花", pref_type="style")
    async with _client(db_session) as client:
        del_resp = await client.delete(
            f"/v1/profile/memory/{row1.id}", headers={"X-User-Id": str(user_id)}
        )
        wrong_resp = await client.post(
            f"/v1/profile/memory/{row2.id}/mark-wrong", headers={"X-User-Id": str(user_id)}
        )
        listed = await client.get("/v1/profile/memory", headers={"X-User-Id": str(user_id)})
    assert del_resp.status_code == 200
    assert wrong_resp.status_code == 200
    assert listed.json()["data"]["preferences"] == []
    actions = set(
        await db_session.scalars(select(AuditLog.action).where(AuditLog.user_id == user_id))
    )
    assert {"memory.deleted", "memory.marked_wrong"} <= actions


async def test_foreign_memory_id_is_404(db_session: AsyncSession, data_trust_on: None) -> None:
    owner = await _seed_user(db_session)
    intruder = await _seed_user(db_session)
    row = await _seed_pref(db_session, owner, "不喜欢亮色")
    async with _client(db_session) as client:
        resp = await client.delete(
            f"/v1/profile/memory/{row.id}", headers={"X-User-Id": str(intruder)}
        )
    assert resp.status_code == 404


async def test_clear_memory_only(db_session: AsyncSession, data_trust_on: None) -> None:
    user_id = await _seed_user(db_session)
    await _seed_pref(db_session, user_id, "不喜欢亮色")
    await _seed_pref(db_session, user_id, "不喜欢碎花", pref_type="style")
    async with _client(db_session) as client:
        resp = await client.delete("/v1/profile/memory", headers={"X-User-Id": str(user_id)})
        listed = await client.get("/v1/profile/memory", headers={"X-User-Id": str(user_id)})
    assert resp.json()["data"]["deleted"] == 2
    assert listed.json()["data"]["preferences"] == []


async def test_clear_all_data_keeps_account_and_audit(
    db_session: AsyncSession, data_trust_on: None
) -> None:
    user_id = await _seed_user(db_session)
    db_session.add(UserSettings(user_id=user_id))
    db_session.add(
        WardrobeItem(
            user_id=user_id,
            title="Shirt",
            category=Category.TOP.value,
            color_main="white",
            status=ItemStatus.WEARABLE.value,
        )
    )
    # async_jobs has user_id WITHOUT an FK — must still be wiped (a stale
    # data-export job would presign the pre-clear bundle).
    db_session.add(
        AsyncJob(
            job_type="user_export",
            idempotency_key=f"user_export:{user_id}",
            user_id=user_id,
            result={"object_key": "exports/old-bundle.zip"},
        )
    )
    await db_session.flush()
    await _seed_pref(db_session, user_id, "不喜欢亮色")

    async with _client(db_session) as client:
        resp = await client.delete("/v1/profile/data", headers={"X-User-Id": str(user_id)})
    assert resp.status_code == 200
    assert resp.json()["data"]["cleared_rows"] >= 4  # settings + item + preference + job

    async def _count(model, column) -> int:
        return await db_session.scalar(
            select(func.count()).select_from(model).where(column == user_id)
        )

    assert await _count(WardrobeItem, WardrobeItem.user_id) == 0
    assert await _count(UserSettings, UserSettings.user_id) == 0
    assert await _count(UserPreferenceMemory, UserPreferenceMemory.user_id) == 0
    assert await _count(AsyncJob, AsyncJob.user_id) == 0
    # Account survives; the audit trail records the action.
    assert await db_session.get(User, user_id) is not None
    actions = set(
        await db_session.scalars(select(AuditLog.action).where(AuditLog.user_id == user_id))
    )
    assert "user.data_cleared" in actions


async def test_flag_off_fails_closed(db_session: AsyncSession) -> None:
    user_id = await _seed_user(db_session)
    async with _client(db_session) as client:
        resp = await client.delete("/v1/profile/data", headers={"X-User-Id": str(user_id)})
    assert resp.status_code == 403
