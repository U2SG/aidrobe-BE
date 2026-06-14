"""Shopping Sense-Check (M17) API tests — flag gating, happy path, history.

Self-built app with get_db_session + the vision dep overridden, mirroring
tests/api/test_outfit_plan.py. The router calls get_settings() directly
(lru-cached, bypasses the dep override), so the flag is monkeypatched on the
consuming module. Vision uses FakeVision (deterministic): "bottom"/"pants" in
the URL → black bottom, otherwise white top.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings
from app.infrastructure.persistence.models.shopping import ShoppingSenseCheck
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.session import get_db_session
from app.infrastructure.vision.fake_vision import FakeVision
from app.presentation.api.v1.routers import shopping
from app.presentation.middlewares.exception_handler import setup_exception_handlers


def _build_app(session: AsyncSession) -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)

    async def _db() -> AsyncSession:
        return session

    app.dependency_overrides[get_db_session] = _db
    app.dependency_overrides[shopping.get_vision_service] = lambda: FakeVision()
    app.include_router(shopping.router)
    return app


def _enable_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        shopping,
        "get_settings",
        lambda: Settings(APP_ENV="dev", ENABLE_SENSE_CHECK=True),
    )


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


async def _seed_user(session: AsyncSession, user_id: uuid.UUID) -> None:
    if await session.scalar(select(User).where(User.id == user_id)) is None:
        session.add(User(id=user_id))
        await session.flush()


async def _add_item(
    session: AsyncSession,
    user_id: uuid.UUID,
    category: str,
    color_main: str,
    *,
    status: str = "wearable",
) -> WardrobeItem:
    item = WardrobeItem(
        user_id=user_id,
        title=f"{color_main} {category}",
        category=category,
        color_main=color_main,
        status=status,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(item)
    await session.flush()
    return item


async def test_flag_off_returns_503(db_session: AsyncSession) -> None:
    app = _build_app(db_session)
    async with _client(app) as client:
        resp = await client.post(
            "/v1/shopping/sense-check",
            headers={"X-User-Id": str(uuid.uuid4())},
            json={"image_url": "http://x/top.jpg"},
        )
    assert resp.status_code == 503


async def test_missing_input_returns_422(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_flag(monkeypatch)
    user_id = uuid.uuid4()
    await _seed_user(db_session, user_id)
    app = _build_app(db_session)
    async with _client(app) as client:
        resp = await client.post(
            "/v1/shopping/sense-check",
            headers={"X-User-Id": str(user_id)},
            json={},
        )
    assert resp.status_code == 422


async def test_skip_when_near_duplicate(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_flag(monkeypatch)
    user_id = uuid.uuid4()
    await _seed_user(db_session, user_id)
    # FakeVision on a generic URL → white top. Seed an identical white top so
    # the candidate is a near-duplicate (same category + color).
    dup = await _add_item(db_session, user_id, "top", "white")
    # Give the user other categories so "top" is NOT a wardrobe gap.
    await _add_item(db_session, user_id, "bottom", "black")
    await _add_item(db_session, user_id, "shoes", "white")

    app = _build_app(db_session)
    async with _client(app) as client:
        resp = await client.post(
            "/v1/shopping/sense-check",
            headers={"X-User-Id": str(user_id)},
            json={"image_url": "http://x/shirt.jpg"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["verdict"] == "skip"
    sim_ids = [s["item_id"] for s in body["similar_items"]]
    assert str(dup.id) in sim_ids
    assert body["similar_items"][0]["similarity"] >= 0.9
    assert "top" not in body["wardrobe_gap"]


async def test_buy_when_fills_gap(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_flag(monkeypatch)
    user_id = uuid.uuid4()
    await _seed_user(db_session, user_id)
    # Empty wardrobe → candidate top fills a gap → buy.
    app = _build_app(db_session)
    async with _client(app) as client:
        resp = await client.post(
            "/v1/shopping/sense-check",
            headers={"X-User-Id": str(user_id)},
            json={"image_url": "http://x/shirt.jpg"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["verdict"] == "buy"
    assert "top" in body["wardrobe_gap"]
    assert body["similar_items"] == []


async def test_buy_when_high_outfit_potential(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_flag(monkeypatch)
    user_id = uuid.uuid4()
    await _seed_user(db_session, user_id)
    # Candidate is a white top (FakeVision). Owns one different-color top (so
    # "top" is not a gap, and no near-duplicate), plus 3 complementary items
    # → outfit_potential >= 3 → buy.
    await _add_item(db_session, user_id, "top", "blue")
    await _add_item(db_session, user_id, "bottom", "black")
    await _add_item(db_session, user_id, "bottom", "navy")
    await _add_item(db_session, user_id, "shoes", "white")

    app = _build_app(db_session)
    async with _client(app) as client:
        resp = await client.post(
            "/v1/shopping/sense-check",
            headers={"X-User-Id": str(user_id)},
            json={"image_url": "http://x/shirt.jpg"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["verdict"] == "buy"
    assert body["outfit_potential"] >= 3
    # blue top is same category, different color → category-only similarity.
    assert body["similar_items"]
    assert body["similar_items"][0]["similarity"] < 0.9


async def test_sense_check_persists_and_history_returns_it(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_flag(monkeypatch)
    user_id = uuid.uuid4()
    await _seed_user(db_session, user_id)
    app = _build_app(db_session)
    async with _client(app) as client:
        post = await client.post(
            "/v1/shopping/sense-check",
            headers={"X-User-Id": str(user_id)},
            json={"image_url": "http://x/shirt.jpg"},
        )
        assert post.status_code == 200, post.text
        hist = await client.get(
            "/v1/shopping/history",
            headers={"X-User-Id": str(user_id)},
        )
    assert hist.status_code == 200, hist.text
    items = hist.json()["items"]
    assert len(items) == 1
    assert items[0]["verdict"] == post.json()["verdict"]
    assert items[0]["image_url"] == "http://x/shirt.jpg"

    # Row persisted to the table.
    row = await db_session.scalar(
        select(ShoppingSenseCheck).where(ShoppingSenseCheck.user_id == user_id)
    )
    assert row is not None
    assert row.verdict == post.json()["verdict"]


async def test_history_flag_off_returns_503(db_session: AsyncSession) -> None:
    app = _build_app(db_session)
    async with _client(app) as client:
        resp = await client.get(
            "/v1/shopping/history",
            headers={"X-User-Id": str(uuid.uuid4())},
        )
    assert resp.status_code == 503
