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

from app.common.enums import ImageType
from app.config.settings import Settings
from app.infrastructure.persistence.models.shopping import ShoppingSenseCheck
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem, WardrobeItemImage
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
    # _is_safe_url does a real DNS lookup; test URLs like http://x/shirt.jpg
    # resolve to gaierror → False. Bypass in tests that need the full pipeline.
    monkeypatch.setattr("app.application.shopping.sense_check._is_safe_url", lambda _url: True)


def _disable_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    # Force flag off regardless of .env (ENABLE_SENSE_CHECK=true in dev .env
    # pollutes the lru_cached get_settings() and breaks flag-off assertions).
    monkeypatch.setattr(
        shopping,
        "get_settings",
        lambda: Settings(APP_ENV="dev", ENABLE_SENSE_CHECK=False),
    )


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


class _SigningStub:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def sign_render_url(
        self,
        key: str,
        *,
        ttl_seconds: int = 1800,
        process: str | None = None,
    ) -> str:
        self.calls.append(key)
        return f"https://signed.local/{key}?token=stub"


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
    title: str | None = None,
    localized_names: dict[str, str] | None = None,
    object_key: str | None = None,
    status: str = "wearable",
) -> WardrobeItem:
    item = WardrobeItem(
        user_id=user_id,
        title=title or f"{color_main} {category}",
        category=category,
        color_main=color_main,
        status=status,
        localized_names=localized_names,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )
    session.add(item)
    await session.flush()
    if object_key is not None:
        session.add(
            WardrobeItemImage(
                item_id=item.id,
                image_type=ImageType.STANDARD.value,
                image_url="https://example.com/standard.jpg",
                object_key=object_key,
                created_at=datetime.now(UTC),
            )
        )
        await session.flush()
    return item


async def test_flag_off_returns_503(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    _disable_flag(monkeypatch)
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
    assert body["display_reason"]["key"] == "sense_check.skip.duplicate"
    assert body["display_reason"]["params"] == {
        "count": 1,
        "color": "white",
        "category": "top",
    }
    assert body["reason"] == body["display_reason"]["fallback_text"]
    assert body["similar_item_count"] == 1


async def test_sense_check_similar_items_include_resolved_name_and_thumbnail(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_flag(monkeypatch)
    signing_stub = _SigningStub()
    monkeypatch.setattr(
        "app.bootstrap.container.object_storage", lambda: signing_stub
    )
    user_id = uuid.uuid4()
    await _seed_user(db_session, user_id)
    item = await _add_item(
        db_session,
        user_id,
        "top",
        "white",
        title="White top",
        object_key="users/u1/items/top-standard.jpg",
    )
    await _add_item(db_session, user_id, "bottom", "black")
    await _add_item(db_session, user_id, "shoes", "black")

    app = _build_app(db_session)
    async with _client(app) as client:
        resp = await client.post(
            "/v1/shopping/sense-check",
            headers={"X-User-Id": str(user_id)},
            json={"image_url": "http://x/shirt.jpg", "locale": "zh-CN"},
        )

    assert resp.status_code == 200, resp.text
    similar = {s["item_id"]: s for s in resp.json()["similar_items"]}
    assert similar[str(item.id)]["name"] == "白色上衣"
    assert similar[str(item.id)]["category"] == "top"
    assert similar[str(item.id)]["color_main"] == "white"
    assert (
        similar[str(item.id)]["thumbnail_url"]
        == "https://signed.local/users/u1/items/top-standard.jpg?token=stub"
    )
    assert signing_stub.calls == ["users/u1/items/top-standard.jpg"]


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
    assert body["display_reason"]["key"] == "sense_check.buy.gap"
    assert body["display_reason"]["params"] == {"category": "top"}
    assert body["reason"] == body["display_reason"]["fallback_text"]


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
    assert body["display_reason"]["key"] == "sense_check.buy.versatile"
    assert body["display_reason"]["params"] == {"category": "top", "count": body["outfit_potential"]}
    assert body["reason"] == body["display_reason"]["fallback_text"]


async def test_consider_when_some_category_but_low_pairability(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_flag(monkeypatch)
    user_id = uuid.uuid4()
    await _seed_user(db_session, user_id)
    await _add_item(db_session, user_id, "top", "blue")

    app = _build_app(db_session)
    async with _client(app) as client:
        resp = await client.post(
            "/v1/shopping/sense-check",
            headers={"X-User-Id": str(user_id)},
            json={"image_url": "http://x/shirt.jpg"},
        )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["verdict"] == "consider"
    assert body["display_reason"]["key"] == "sense_check.consider.some"
    assert body["display_reason"]["params"] == {"category": "top", "count": 0}
    assert body["similar_item_count"] == 0


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
    assert items[0]["display_reason"] == post.json()["display_reason"]
    assert items[0]["similar_item_count"] == post.json()["similar_item_count"]

    # Row persisted to the table.
    row = await db_session.scalar(
        select(ShoppingSenseCheck).where(ShoppingSenseCheck.user_id == user_id)
    )
    assert row is not None
    assert row.verdict == post.json()["verdict"]
    assert row.object_key is None
    assert row.payload["display_reason"] == post.json()["display_reason"]
    assert row.payload["similar_item_count"] == post.json()["similar_item_count"]


async def test_sense_check_persists_internal_object_key_and_history_signs(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_flag(monkeypatch)
    signing_stub = _SigningStub()
    monkeypatch.setattr(
        "app.bootstrap.container.object_storage", lambda: signing_stub
    )
    user_id = uuid.uuid4()
    await _seed_user(db_session, user_id)
    app = _build_app(db_session)
    image_url = "http://localhost:9000/local/files/wardrobe/abc/shirt.jpg"

    async with _client(app) as client:
        post = await client.post(
            "/v1/shopping/sense-check",
            headers={"X-User-Id": str(user_id)},
            json={"image_url": image_url},
        )
        assert post.status_code == 200, post.text
        hist = await client.get(
            "/v1/shopping/history",
            headers={"X-User-Id": str(user_id)},
        )

    assert hist.status_code == 200, hist.text
    items = hist.json()["items"]
    assert (
        items[0]["image_url"]
        == "https://signed.local/wardrobe/abc/shirt.jpg?token=stub"
    )
    assert signing_stub.calls == ["wardrobe/abc/shirt.jpg"]

    row = await db_session.scalar(
        select(ShoppingSenseCheck).where(ShoppingSenseCheck.user_id == user_id)
    )
    assert row is not None
    assert row.object_key == "wardrobe/abc/shirt.jpg"


async def test_history_with_legacy_raw_image_url_does_not_sign(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    _enable_flag(monkeypatch)
    signing_stub = _SigningStub()
    monkeypatch.setattr(
        "app.bootstrap.container.object_storage", lambda: signing_stub
    )
    user_id = uuid.uuid4()
    await _seed_user(db_session, user_id)
    record = ShoppingSenseCheck(
        user_id=user_id,
        image_url="https://cdn.example.com/raw.jpg",
        object_key=None,
        source_url=None,
        verdict="consider",
        reason="Looks usable.",
        payload={"similar_items": [], "wardrobe_gap": [], "outfit_potential": 0},
    )
    db_session.add(record)
    await db_session.flush()

    app = _build_app(db_session)
    async with _client(app) as client:
        hist = await client.get(
            "/v1/shopping/history",
            headers={"X-User-Id": str(user_id)},
        )

    assert hist.status_code == 200, hist.text
    assert hist.json()["items"][0]["image_url"] == "https://cdn.example.com/raw.jpg"
    assert signing_stub.calls == []


async def test_history_flag_off_returns_503(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    _disable_flag(monkeypatch)
    app = _build_app(db_session)
    async with _client(app) as client:
        resp = await client.get(
            "/v1/shopping/history",
            headers={"X-User-Id": str(uuid.uuid4())},
        )
    assert resp.status_code == 503
