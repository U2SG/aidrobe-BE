"""V2 gap-fill surfaces: wardrobe list filters, standalone item wears,
insights sufficiency fields, commute_exposure round-trip, data-export
polling."""

import uuid
from datetime import date, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.local_time import local_datetime, utc_now
from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import account as account_router
from app.presentation.api.v1.routers import insights as insights_router
from app.presentation.api.v1.routers import settings as settings_router
from app.presentation.api.v1.routers import wardrobe as wardrobe_router
from app.presentation.middlewares.exception_handler import setup_exception_handlers


def build_app(session: AsyncSession) -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)

    async def override_db() -> AsyncSession:
        return session

    def override_settings() -> Settings:
        return Settings(APP_ENV="dev")

    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[get_settings] = override_settings
    app.include_router(wardrobe_router.router)
    app.include_router(insights_router.router)
    app.include_router(settings_router.router)
    app.include_router(account_router.router)
    return app


def _local_today() -> date:
    return local_datetime(utc_now(), None, Settings().DEFAULT_TIMEZONE).date()


async def _seed_user(session: AsyncSession, user_id: uuid.UUID) -> None:
    session.add(User(id=user_id))
    await session.flush()


async def _seed_item(session: AsyncSession, user_id: uuid.UUID, **kw: object) -> WardrobeItem:
    item = WardrobeItem(
        user_id=user_id,
        title=kw.pop("title", "Item"),
        category=kw.pop("category", "top"),
        color_main=kw.pop("color_main", "black"),
        **kw,  # type: ignore[arg-type]
    )
    session.add(item)
    await session.flush()
    return item


# ---------------------------------------------------------------------------
# Wardrobe list filters


async def test_wardrobe_list_filters_status_season_color(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    await _seed_user(db_session, user_id)
    await _seed_item(
        db_session,
        user_id,
        title="Blue spring tee",
        color_main="blue",
        status="wearable",
        season_tags={"tags": ["spring", "summer"]},
    )
    await _seed_item(
        db_session,
        user_id,
        title="Black winter coat",
        category="outerwear",
        color_main="black",
        status="dirty",
        season_tags={"tags": ["winter"]},
    )
    app = build_app(db_session)
    headers = {"X-User-Id": str(user_id)}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        by_status = await client.get("/v1/wardrobe-items?status=dirty", headers=headers)
        # season matches the persisted {"tags": [...]} JSONB shape
        by_season = await client.get("/v1/wardrobe-items?season=spring", headers=headers)
        by_color = await client.get("/v1/wardrobe-items?color=black", headers=headers)
        combined = await client.get(
            "/v1/wardrobe-items?status=wearable&season=summer&color=blue", headers=headers
        )

    assert by_status.status_code == 200
    assert by_status.json()["data"]["total"] == 1
    assert by_status.json()["data"]["items"][0]["title"] == "Black winter coat"

    assert by_season.json()["data"]["total"] == 1
    assert by_season.json()["data"]["items"][0]["title"] == "Blue spring tee"

    assert by_color.json()["data"]["total"] == 1
    assert by_color.json()["data"]["items"][0]["title"] == "Black winter coat"

    assert combined.json()["data"]["total"] == 1
    assert combined.json()["data"]["items"][0]["title"] == "Blue spring tee"


async def test_season_filter_includes_all_season_items(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    await _seed_user(db_session, user_id)
    await _seed_item(db_session, user_id, title="Wool trousers", season_tags={"tags": ["all_season"]})
    await _seed_item(db_session, user_id, title="Winter coat", category="outerwear", season_tags={"tags": ["winter"]})
    app = build_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.get("/v1/wardrobe-items?season=summer", headers={"X-User-Id": str(user_id)})

    assert [item["title"] for item in response.json()["data"]["items"]] == ["Wool trousers"]
    assert response.json()["data"]["items"][0]["season_tags"] == ["all_season"]


@pytest.mark.parametrize("query", ["status=bogus", "season=rainy"])
async def test_wardrobe_list_rejects_invalid_filter_values(
    db_session: AsyncSession, query: str
) -> None:
    user_id = uuid.uuid4()
    await _seed_user(db_session, user_id)
    app = build_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get(
            f"/v1/wardrobe-items?{query}", headers={"X-User-Id": str(user_id)}
        )

    assert resp.status_code == 422
    assert resp.json()["code"] == 4001


# ---------------------------------------------------------------------------
# Standalone item wears


async def test_mark_item_worn_increments_and_is_idempotent_per_day(
    db_session: AsyncSession,
) -> None:
    user_id = uuid.uuid4()
    await _seed_user(db_session, user_id)
    item = await _seed_item(db_session, user_id, title="Shirt")
    app = build_app(db_session)
    headers = {"X-User-Id": str(user_id)}
    today = _local_today()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        first = await client.post(f"/v1/wardrobe-items/{item.id}/wears", headers=headers)
        duplicate = await client.post(f"/v1/wardrobe-items/{item.id}/wears", headers=headers)

    assert first.status_code == 200
    assert first.json()["data"]["wear_count"] == 1
    assert first.json()["data"]["last_worn"] == today.isoformat()
    # Same item + same day → no double-count (mirrors the outfit wear path).
    assert duplicate.status_code == 200
    assert duplicate.json()["data"]["wear_count"] == 1

    count = await db_session.scalar(
        select(WardrobeItem.wear_count).where(WardrobeItem.id == item.id)
    )
    assert count == 1


async def test_mark_item_worn_backfill_keeps_last_worn_forward_only(
    db_session: AsyncSession,
) -> None:
    user_id = uuid.uuid4()
    await _seed_user(db_session, user_id)
    item = await _seed_item(db_session, user_id, title="Shirt")
    app = build_app(db_session)
    headers = {"X-User-Id": str(user_id)}
    today = _local_today()
    yesterday = today - timedelta(days=1)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        await client.post(f"/v1/wardrobe-items/{item.id}/wears", headers=headers)
        backfill = await client.post(
            f"/v1/wardrobe-items/{item.id}/wears",
            headers=headers,
            json={"worn_date": yesterday.isoformat()},
        )
        history = await client.get(
            f"/v1/wardrobe-items/{item.id}/wear-history", headers=headers
        )

    assert backfill.status_code == 200
    data = backfill.json()["data"]
    assert data["wear_count"] == 2
    # Backfilling an earlier date must not move last_worn backwards.
    assert data["last_worn"] == today.isoformat()

    # Manual wears surface in wear-history so it stays consistent with wear_count.
    hist = history.json()["data"]
    assert hist["daily_counts"][today.isoformat()] == 1
    assert hist["daily_counts"][yesterday.isoformat()] == 1
    assert all(e["source"] == "manual" and e["outfit_id"] is None for e in hist["entries"])
    assert len(hist["entries"]) == 2


async def test_mark_item_worn_404_for_foreign_item(db_session: AsyncSession) -> None:
    owner_id = uuid.uuid4()
    intruder_id = uuid.uuid4()
    await _seed_user(db_session, owner_id)
    await _seed_user(db_session, intruder_id)
    item = await _seed_item(db_session, owner_id)
    app = build_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.post(
            f"/v1/wardrobe-items/{item.id}/wears",
            headers={"X-User-Id": str(intruder_id)},
        )

    assert resp.status_code == 404
    assert resp.json()["code"] == 4041


# ---------------------------------------------------------------------------
# Insights sufficiency fields


async def test_insights_sufficiency_boundary(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pin the flag off regardless of local .env — get_settings is lru_cached,
    # so patch it on the consuming module.
    monkeypatch.setattr(
        "app.application.insights.wardrobe_insights.get_settings",
        lambda: Settings(APP_ENV="dev", ENABLE_WARDROBE_REPORT=False),
    )
    user_id = uuid.uuid4()
    await _seed_user(db_session, user_id)
    await _seed_item(db_session, user_id, title="A", wear_count=9)
    app = build_app(db_session)
    headers = {"X-User-Id": str(user_id)}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        below = await client.get("/v1/insights/summary", headers=headers)
        await _seed_item(db_session, user_id, title="B", wear_count=1)
        at_threshold = await client.get("/v1/insights/summary", headers=headers)

    # V3 insights endpoints return data directly (no envelope) — intentional.
    below_data = below.json()
    assert below_data["wear_history_count"] == 9
    assert below_data["has_sufficient_wear_history"] is False
    assert below_data["report_available"] is False

    at_data = at_threshold.json()
    assert at_data["wear_history_count"] == 10
    assert at_data["has_sufficient_wear_history"] is True
    # Flag is off by default, so the report stays gated even with enough data.
    assert at_data["report_available"] is False


async def test_insights_report_available_follows_flag(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # get_settings is lru_cached — patch it on the consuming module.
    monkeypatch.setattr(
        "app.application.insights.wardrobe_insights.get_settings",
        lambda: Settings(APP_ENV="dev", ENABLE_WARDROBE_REPORT=True),
    )
    user_id = uuid.uuid4()
    await _seed_user(db_session, user_id)
    await _seed_item(db_session, user_id, wear_count=10)
    app = build_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get("/v1/insights/summary", headers={"X-User-Id": str(user_id)})

    data = resp.json()
    assert data["has_sufficient_wear_history"] is True
    assert data["report_available"] is True


async def test_insights_empty_wardrobe_reports_insufficient(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    await _seed_user(db_session, user_id)
    app = build_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.get("/v1/insights/summary", headers={"X-User-Id": str(user_id)})

    data = resp.json()
    assert data["wear_history_count"] == 0
    assert data["has_sufficient_wear_history"] is False
    assert data["report_available"] is False


# ---------------------------------------------------------------------------
# commute_exposure


async def test_commute_exposure_round_trip_and_clear(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    app = build_app(db_session)
    headers = {"X-User-Id": str(user_id)}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        set_resp = await client.patch(
            "/v1/user/settings", headers=headers, json={"commute_exposure": "more_outdoor"}
        )
        get_resp = await client.get("/v1/user/settings", headers=headers)
        clear_resp = await client.patch(
            "/v1/user/settings", headers=headers, json={"commute_exposure": None}
        )
        cleared_get = await client.get("/v1/user/settings", headers=headers)

    assert set_resp.status_code == 200
    assert set_resp.json()["data"]["commute_exposure"] == "more_outdoor"
    assert get_resp.json()["data"]["commute_exposure"] == "more_outdoor"
    # Explicit null clears the answer (the *_set pattern).
    assert clear_resp.status_code == 200
    assert cleared_get.json()["data"].get("commute_exposure") is None


async def test_commute_exposure_rejects_invalid_value(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    app = build_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.patch(
            "/v1/user/settings",
            headers={"X-User-Id": str(user_id)},
            json={"commute_exposure": "sometimes"},
        )

    assert resp.status_code == 422
    assert resp.json()["code"] == 4001


# ---------------------------------------------------------------------------
# Data-export polling


class _FakeSigningStorage:
    def sign_render_url(self, key: str, *, ttl_seconds: int = 1800, process: str | None = None) -> str:
        return f"https://signed.example/{key}?ttl={ttl_seconds}"


async def _seed_export_job(
    session: AsyncSession, user_id: uuid.UUID, *, status: str, result: dict | None = None
) -> AsyncJob:
    job = AsyncJob(
        job_type="user_export",
        idempotency_key=f"user_export:{user_id}:{uuid.uuid4()}",
        user_id=user_id,
        target_id=user_id,
        status=status,
        payload={"user_id": str(user_id)},
        result=result,
    )
    session.add(job)
    await session.flush()
    return job


async def test_export_polling_states(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(account_router, "object_storage", lambda: _FakeSigningStorage())
    user_id = uuid.uuid4()
    await _seed_user(db_session, user_id)
    queued = await _seed_export_job(db_session, user_id, status="queued")
    succeeded = await _seed_export_job(
        db_session, user_id, status="succeeded", result={"object_key": f"users/{user_id}/export/x.json"}
    )
    failed = await _seed_export_job(db_session, user_id, status="failed")
    app = build_app(db_session)
    headers = {"X-User-Id": str(user_id)}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        queued_resp = await client.get(f"/v1/me/data-export/{queued.id}", headers=headers)
        ready_resp = await client.get(f"/v1/me/data-export/{succeeded.id}", headers=headers)
        failed_resp = await client.get(f"/v1/me/data-export/{failed.id}", headers=headers)

    q = queued_resp.json()["data"]
    assert q["status"] == "queued"
    assert q["download_url"] is None
    assert "prepared" in q["message"]

    r = ready_resp.json()["data"]
    assert r["status"] == "succeeded"
    # Signed at read time from the stored object_key — never persisted.
    assert r["download_url"].startswith(f"https://signed.example/users/{user_id}/export/")
    assert "expires" in r["message"]

    f = failed_resp.json()["data"]
    assert f["status"] == "failed"
    assert "failed" in f["message"].lower()


async def test_export_polling_404_for_foreign_or_wrong_type_job(
    db_session: AsyncSession,
) -> None:
    owner_id = uuid.uuid4()
    intruder_id = uuid.uuid4()
    await _seed_user(db_session, owner_id)
    await _seed_user(db_session, intruder_id)
    export_job = await _seed_export_job(db_session, owner_id, status="queued")
    other_type_job = AsyncJob(
        job_type="embedding",
        idempotency_key=f"embedding:{uuid.uuid4()}:v1",
        user_id=owner_id,
        status="queued",
    )
    db_session.add(other_type_job)
    await db_session.flush()
    app = build_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        foreign = await client.get(
            f"/v1/me/data-export/{export_job.id}", headers={"X-User-Id": str(intruder_id)}
        )
        wrong_type = await client.get(
            f"/v1/me/data-export/{other_type_job.id}", headers={"X-User-Id": str(owner_id)}
        )

    assert foreign.status_code == 404
    assert wrong_type.status_code == 404


async def test_patch_persists_and_returns_the_extended_attributes(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    await _seed_user(db_session, user_id)
    item = await _seed_item(db_session, user_id, title="Jacket", category="outerwear")
    app = build_app(db_session)
    headers = {"X-User-Id": str(user_id)}
    changes = {
        "sub_category": "bomber",
        "color_secondary": "black",
        "material": "leather",
        "season_tags": ["autumn", "winter"],
        "style_tags": ["casual", "street"],
        "formality_score": 30,
        "time_cost_level": "low",
        "purchase_price": 899.5,
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        patched = await client.patch(f"/v1/wardrobe-items/{item.id}", headers=headers, json=changes)
        fetched = await client.get(f"/v1/wardrobe-items/{item.id}", headers=headers)

    assert patched.status_code == 200
    for body in (patched.json()["data"], fetched.json()["data"]):
        for key, value in changes.items():
            assert body[key] == value, key
    await db_session.refresh(item)
    assert item.season_tags == {"tags": ["autumn", "winter"]}


async def test_patch_empty_tag_list_clears_and_omitted_fields_stay(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    await _seed_user(db_session, user_id)
    item = await _seed_item(
        db_session, user_id, season_tags={"tags": ["summer"]}, material="linen", formality_score=50
    )
    app = build_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.patch(
            f"/v1/wardrobe-items/{item.id}", headers={"X-User-Id": str(user_id)}, json={"season_tags": []}
        )

    body = response.json()["data"]
    assert body["season_tags"] is None
    assert body["material"] == "linen"
    assert body["formality_score"] == 50


@pytest.mark.parametrize(
    "change",
    [{"season_tags": ["rainy"]}, {"formality_score": 101}, {"formality_score": -1}, {"style_tags": ["x" * 25]}],
)
async def test_patch_rejects_out_of_vocabulary_attributes(db_session: AsyncSession, change: dict[str, object]) -> None:
    user_id = uuid.uuid4()
    await _seed_user(db_session, user_id)
    item = await _seed_item(db_session, user_id)
    app = build_app(db_session)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.patch(f"/v1/wardrobe-items/{item.id}", headers={"X-User-Id": str(user_id)}, json=change)

    assert response.status_code == 422
