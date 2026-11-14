"""HTTP contract for the deterministic outfit-board share producer."""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import ItemStatus, RecommendationStatus, VisualType
from app.config.settings import Settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.outfit_visual import OutfitVisualAsset
from app.infrastructure.persistence.models.recommendation import (
    DailyRecommendation,
    Outfit,
    OutfitItem,
)
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem, WardrobeItemImage
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import outfit_visual
from app.presentation.middlewares.exception_handler import setup_exception_handlers


def _app(session: AsyncSession) -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)

    async def _db() -> AsyncSession:
        return session

    app.dependency_overrides[get_db_session] = _db
    app.include_router(outfit_visual.router)
    return app


async def _seed_outfit(session: AsyncSession, user_id: uuid.UUID) -> uuid.UUID:
    now = datetime.now(UTC)
    session.add(User(id=user_id))
    await session.flush()
    daily = DailyRecommendation(
        user_id=user_id,
        recommendation_date=date(2026, 8, 29),
        status=RecommendationStatus.READY.value,
        generation_source="test",
    )
    session.add(daily)
    await session.flush()
    outfit = Outfit(
        daily_recommendation_id=daily.id,
        user_id=user_id,
        outfit_type="safe",
        title="Shareable outfit",
        reason_text="Test fixture",
        score_total=Decimal("1.0"),
        rank_order=1,
        created_at=now,
    )
    top = WardrobeItem(
        user_id=user_id,
        title="Top",
        category="top",
        color_main="white",
        status=ItemStatus.WEARABLE.value,
        cover_image_url="https://storage.example/top-cover.jpg",
    )
    bottom = WardrobeItem(
        user_id=user_id,
        title="Bottom",
        category="bottom",
        color_main="black",
        status=ItemStatus.WEARABLE.value,
        original_image_url="https://storage.example/bottom-original.jpg",
    )
    session.add_all([outfit, top, bottom])
    await session.flush()
    session.add_all(
        [
            OutfitItem(outfit_id=outfit.id, item_id=top.id, role="top", display_order=1, created_at=now),
            OutfitItem(outfit_id=outfit.id, item_id=bottom.id, role="bottom", display_order=2, created_at=now),
            WardrobeItemImage(
                item_id=top.id,
                image_type="cutout",
                image_url="https://storage.example/top-cutout.png",
                object_key=f"users/{user_id}/items/{top.id}/cutout.png",
                created_at=now,
            ),
            WardrobeItemImage(
                item_id=top.id,
                image_type="thumbnail",
                image_url="https://storage.example/top-thumb.jpg",
                object_key=f"users/{user_id}/items/{top.id}/thumb.jpg",
                created_at=now,
            ),
            WardrobeItemImage(
                item_id=bottom.id,
                image_type="original",
                image_url="https://external.example/bottom-original.jpg",
                object_key=None,
                created_at=now,
            ),
        ]
    )
    await session.flush()
    return outfit.id


async def _post(client: AsyncClient, outfit_id: uuid.UUID, user_id: uuid.UUID):
    return await client.post(f"/v1/outfits/{outfit_id}/share-asset", headers={"X-User-Id": str(user_id)})


async def test_share_asset_queues_a_pollable_worker_compatible_board(
    db_session: AsyncSession,
) -> None:
    user_id = uuid.uuid4()
    outfit_id = await _seed_outfit(db_session, user_id)

    async with AsyncClient(transport=ASGITransport(app=_app(db_session)), base_url="http://testserver") as client:
        response = await _post(client, outfit_id, user_id)

    assert response.status_code == 202, response.text
    assert response.json() == {"asset_id": response.json()["asset_id"], "generation_status": "queued"}
    asset = (await db_session.scalars(select(OutfitVisualAsset))).one()
    job = (await db_session.scalars(select(AsyncJob))).one()
    assert asset.id == uuid.UUID(response.json()["asset_id"])
    assert asset.user_id == user_id
    assert asset.outfit_id == outfit_id
    assert asset.visual_type == VisualType.OUTFIT_BOARD.value
    assert asset.input_snapshot == {
        "item_list": [
            {
                "role": "top",
                "category": "top",
                "cutout_object_key": asset.input_snapshot["item_list"][0]["cutout_object_key"],
                "thumbnail_object_key": asset.input_snapshot["item_list"][0]["thumbnail_object_key"],
            },
            {
                "role": "bottom",
                "category": "bottom",
                "thumbnail_url": "https://external.example/bottom-original.jpg",
            },
        ]
    }
    top_snapshot = asset.input_snapshot["item_list"][0]
    assert top_snapshot["cutout_object_key"].startswith(f"users/{user_id}/items/")
    assert top_snapshot["cutout_object_key"].endswith("/cutout.png")
    assert top_snapshot["thumbnail_object_key"].endswith("/thumb.jpg")
    assert "cutout_image_url" not in top_snapshot
    assert "thumbnail_url" not in top_snapshot
    snapshot_text = str(asset.input_snapshot).lower()
    for forbidden in ("selfie", "prompt", "reason_text", "title", "email"):
        assert forbidden not in snapshot_text
    assert job.target_id == asset.id
    assert job.idempotency_key == f"outfit_board:{outfit_id}:v2"
    assert job.payload == {
        "asset_id": str(asset.id),
        "layout_version": "v2",
        "outfit_id": str(outfit_id),
    }


async def test_share_asset_reuses_the_same_asset_and_job(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    outfit_id = await _seed_outfit(db_session, user_id)

    async with AsyncClient(transport=ASGITransport(app=_app(db_session)), base_url="http://testserver") as client:
        first = await _post(client, outfit_id, user_id)
        second = await _post(client, outfit_id, user_id)

    assert first.status_code == second.status_code == 202
    assert first.json()["asset_id"] == second.json()["asset_id"]
    assert await db_session.scalar(select(func.count()).select_from(OutfitVisualAsset)) == 1
    assert await db_session.scalar(select(func.count()).select_from(AsyncJob)) == 1


async def test_share_asset_hides_foreign_and_missing_outfits(db_session: AsyncSession) -> None:
    owner_id = uuid.uuid4()
    outfit_id = await _seed_outfit(db_session, owner_id)
    other_id = uuid.uuid4()

    async with AsyncClient(transport=ASGITransport(app=_app(db_session)), base_url="http://testserver") as client:
        foreign = await _post(client, outfit_id, other_id)
        missing = await _post(client, uuid.uuid4(), other_id)

    assert foreign.status_code == missing.status_code == 404
    assert foreign.json()["code"] == missing.json()["code"] == 4041
    assert await db_session.scalar(select(func.count()).select_from(OutfitVisualAsset)) == 0
    assert await db_session.scalar(select(func.count()).select_from(AsyncJob)) == 0


async def test_gpt_outfit_board_uses_a_separate_reusable_asset(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        outfit_visual,
        "get_settings",
        lambda: Settings(ENABLE_ITEM_CUTOUT=True, IMAGE_CUTOUT_PROVIDER="openai", OPENAI_API_KEY="test"),
    )

    async def allow(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(outfit_visual, "assert_within_quota", allow)
    user_id = uuid.uuid4()
    outfit_id = await _seed_outfit(db_session, user_id)
    path = f"/v1/outfits/{outfit_id}/share-asset"
    headers = {"X-User-Id": str(user_id)}
    async with AsyncClient(transport=ASGITransport(app=_app(db_session)), base_url="http://testserver") as client:
        assert (await client.get(path, headers=headers)).status_code == 404
        board = await client.post(path, headers=headers)
        generated = await client.post(path, headers=headers, params={"generate": "true"})
        assert generated.status_code == 202
        assert board.json()["asset_id"] != generated.json()["asset_id"]
        lookup = await client.get(path, headers=headers)
        assert lookup.json()["asset_id"] == generated.json()["asset_id"]
        replay = await client.post(path, headers=headers, params={"generate": "true"})
        assert replay.json()["asset_id"] == generated.json()["asset_id"]

    job = (await db_session.scalars(select(AsyncJob).where(AsyncJob.idempotency_key.endswith(":gpt-v1")))).one()
    asset = await db_session.get(OutfitVisualAsset, job.target_id)
    assert asset is not None and asset.input_snapshot["generate"] is True
