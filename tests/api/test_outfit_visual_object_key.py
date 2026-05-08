"""Phase E5 — object_key-driven signed render URL on the read paths.

Asserts that when an asset / image row has `object_key` set, the API
response surfaces a signed URL produced by the storage backend rather
than the persisted (TTL-bound) `image_url`."""

from __future__ import annotations

import uuid

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.bootstrap.container import object_storage
from app.common.enums import Category, ItemStatus
from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.outfit_visual import OutfitVisualAsset
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem, WardrobeItemImage
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import outfit_visual as visual_router
from app.presentation.middlewares.exception_handler import setup_exception_handlers


class _SigningStub:
    """Storage stub that records `sign_render_url` calls and returns a
    deterministic, recognizable URL so tests can assert it was used."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, int, str | None]] = []

    def sign_render_url(
        self,
        key: str,
        *,
        ttl_seconds: int = 1800,
        process: str | None = None,
    ) -> str:
        self.calls.append((key, ttl_seconds, process))
        return f"https://signed.local/{key}?token=stub"


def _build_app(session: AsyncSession) -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)

    async def override_db() -> AsyncSession:
        return session

    def override_settings() -> Settings:
        return Settings(APP_ENV="dev")

    app.dependency_overrides[get_db_session] = override_db
    app.dependency_overrides[get_settings] = override_settings
    app.include_router(visual_router.router)
    return app


@pytest.fixture
def signing_stub(monkeypatch: pytest.MonkeyPatch) -> _SigningStub:
    stub = _SigningStub()
    monkeypatch.setattr(
        "app.bootstrap.container.object_storage", lambda: stub
    )
    return stub


async def test_visual_asset_response_signs_when_object_key_present(
    db_session: AsyncSession, signing_stub: _SigningStub
) -> None:
    user_id = uuid.uuid4()
    db_session.add(User(id=user_id))
    asset = OutfitVisualAsset(
        user_id=user_id,
        generation_status="completed",
        visual_type="outfit_board",
        image_url="https://baked-in/old.jpg",  # legacy field
        object_key=f"{user_id}/outfit_boards/abc.jpg",
    )
    db_session.add(asset)
    await db_session.flush()

    app = _build_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.get(
            f"/v1/outfit-visual-assets/{asset.id}",
            headers={"X-User-Id": str(user_id)},
        )
    assert resp.status_code == 200
    body = resp.json()
    # Signed URL replaced the baked-in URL.
    assert body["image_url"] == f"https://signed.local/{user_id}/outfit_boards/abc.jpg?token=stub"
    # The signing call carries the right key.
    assert signing_stub.calls[0][0] == f"{user_id}/outfit_boards/abc.jpg"


async def test_visual_asset_response_falls_back_when_no_object_key(
    db_session: AsyncSession, signing_stub: _SigningStub
) -> None:
    """Legacy rows without object_key keep returning their persisted URL.
    The signing path must NOT fire (avoids signing for objects we don't
    own — e.g. import-flow originals)."""
    user_id = uuid.uuid4()
    db_session.add(User(id=user_id))
    asset = OutfitVisualAsset(
        user_id=user_id,
        generation_status="completed",
        visual_type="outfit_board",
        image_url="https://external/legacy.jpg",
        object_key=None,
    )
    db_session.add(asset)
    await db_session.flush()

    app = _build_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.get(
            f"/v1/outfit-visual-assets/{asset.id}",
            headers={"X-User-Id": str(user_id)},
        )
    assert resp.status_code == 200
    assert resp.json()["image_url"] == "https://external/legacy.jpg"
    assert signing_stub.calls == []


async def test_visual_asset_response_falls_back_when_signing_raises(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If signing fails (e.g. storage misconfigured at deploy time), the
    read MUST NOT 500 — drop to the persisted URL so users keep seeing
    something rather than a hard error."""

    class _ExplodingStorage:
        def sign_render_url(self, key: str, **kwargs: object) -> str:
            raise RuntimeError("sts down")

    monkeypatch.setattr(
        "app.bootstrap.container.object_storage", lambda: _ExplodingStorage()
    )

    user_id = uuid.uuid4()
    db_session.add(User(id=user_id))
    asset = OutfitVisualAsset(
        user_id=user_id,
        generation_status="completed",
        visual_type="outfit_board",
        image_url="https://baked-in/fallback.jpg",
        object_key="some/key.jpg",
    )
    db_session.add(asset)
    await db_session.flush()

    app = _build_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.get(
            f"/v1/outfit-visual-assets/{asset.id}",
            headers={"X-User-Id": str(user_id)},
        )
    assert resp.status_code == 200
    assert resp.json()["image_url"] == "https://baked-in/fallback.jpg"


async def test_item_images_response_signs_each_internal_variant(
    db_session: AsyncSession, signing_stub: _SigningStub
) -> None:
    """The wardrobe-items/{id}/images endpoint mixes external (import)
    rows and internal (image_worker) rows. Internal rows have
    object_key and should be signed; external rows pass through."""
    user_id = uuid.uuid4()
    db_session.add(User(id=user_id))
    item = WardrobeItem(
        user_id=user_id,
        title="t",
        category=Category.TOP.value,
        color_main="white",
        status=ItemStatus.WEARABLE.value,
    )
    db_session.add(item)
    await db_session.flush()

    # External (import) original — no object_key.
    db_session.add(
        WardrobeItemImage(
            item_id=item.id,
            image_type="original",
            image_url="https://external/import.jpg",
            object_key=None,
        )
    )
    # Internal cutout produced by image_worker — has object_key.
    db_session.add(
        WardrobeItemImage(
            item_id=item.id,
            image_type="cutout",
            image_url="https://stale-baked-in/cutout.png",
            object_key=f"{user_id}/{item.id}/cutout.png",
        )
    )
    await db_session.flush()

    app = _build_app(db_session)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver"
    ) as client:
        resp = await client.get(
            f"/v1/wardrobe-items/{item.id}/images",
            headers={"X-User-Id": str(user_id)},
        )
    assert resp.status_code == 200
    by_type = {img["image_type"]: img["image_url"] for img in resp.json()["images"]}
    assert by_type["original"] == "https://external/import.jpg"
    assert by_type["cutout"].startswith("https://signed.local/")
    # Only the internal row triggered signing.
    assert len(signing_stub.calls) == 1


# Silence "imported but unused" for the storage helper we patch around.
_ = object_storage
