"""HTTP contract for the deterministic outfit-board share producer."""

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.common.enums import CutoutStatus, ItemStatus, RecommendationStatus, VisualType
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
        lambda: Settings(
            ENABLE_ITEM_CUTOUT=True, IMAGE_CUTOUT_PROVIDER="openai", OPENAI_API_KEY="test", QWEN_API_KEY="test"
        ),
    )

    async def allow(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(outfit_visual, "assert_within_quota", allow)
    user_id = uuid.uuid4()
    outfit_id = await _seed_outfit(db_session, user_id)
    old_asset = OutfitVisualAsset(
        user_id=user_id,
        outfit_id=outfit_id,
        visual_type=VisualType.OUTFIT_BOARD.value,
        generation_status="completed",
        image_url="https://storage.example/old.png",
        input_snapshot={"generate": True},
    )
    db_session.add(old_asset)
    await db_session.flush()
    db_session.add(
        AsyncJob(
            job_type="outfit_board",
            idempotency_key=f"outfit_board:{outfit_id}:gpt-v1",
            target_id=old_asset.id,
            user_id=user_id,
            payload={"layout_version": "gpt-v1"},
        )
    )
    await db_session.flush()
    path = f"/v1/outfits/{outfit_id}/share-asset"
    headers = {"X-User-Id": str(user_id)}
    async with AsyncClient(transport=ASGITransport(app=_app(db_session)), base_url="http://testserver") as client:
        assert (await client.get(path, headers=headers)).status_code == 404
        legacy = await client.get(f"/v1/outfit-visual-assets/{old_asset.id}", headers=headers)
        assert legacy.json()["generation_status"] == "rejected_by_quality_gate"
        assert legacy.json()["image_url"] is None
        board = await client.post(path, headers=headers)
        generated = await client.post(path, headers=headers, params={"generate": "true"})
        assert generated.status_code == 202
        assert board.json()["asset_id"] != generated.json()["asset_id"]
        lookup = await client.get(path, headers=headers)
        assert lookup.json()["asset_id"] == generated.json()["asset_id"]
        replay = await client.post(path, headers=headers, params={"generate": "true"})
        assert replay.json()["asset_id"] == generated.json()["asset_id"]

    job = (await db_session.scalars(
        select(AsyncJob).where(AsyncJob.target_id == uuid.UUID(generated.json()["asset_id"]))
    )).one()
    assert job.payload["layout_version"] == outfit_visual._GENERATED_OUTFIT_LAYOUT_VERSION
    asset = await db_session.get(OutfitVisualAsset, job.target_id)
    assert asset is not None and asset.input_snapshot["generate"] is True



async def test_generated_board_reviews_the_single_garment_crop(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    outfit_id = await _seed_outfit(db_session, user_id)
    top = await db_session.scalar(
        select(WardrobeItem).where(WardrobeItem.user_id == user_id, WardrobeItem.category == "top")
    )
    crop_key = f"users/{user_id}/wardrobe/verified-crops/top.jpg"
    db_session.add(
        WardrobeItemImage(
            item_id=top.id,
            image_type="crop",
            image_url=f"https://storage.example/{crop_key}",
            object_key=crop_key,
            created_at=datetime.now(UTC),
        )
    )
    await db_session.flush()

    items = await outfit_visual._outfit_board_item_list(db_session, outfit_id=outfit_id, user_id=user_id, generate=True)
    assert items[0]["cutout_object_key"].endswith("/cutout.png")
    assert items[0]["review_object_key"] == crop_key


@pytest.mark.parametrize("status,result,current,owned,uses_crop", [
    ("succeeded", None, True, True, True),
    ("running", None, True, True, False),
    ("succeeded", {"review": "rejected"}, True, True, False),
    ("succeeded", None, False, True, False),
    ("succeeded", None, True, False, False),
])
async def test_generated_board_keeps_legacy_crop_only_for_a_current_reviewed_cutout(
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    status: str,
    result: dict[str, str] | None,
    current: bool,
    owned: bool,
    uses_crop: bool,
) -> None:
    base = "http://localhost:9000/local"
    monkeypatch.setattr(outfit_visual, "get_settings", lambda: Settings(STORAGE_PROVIDER="local"))
    user_id = uuid.uuid4()
    outfit_id = await _seed_outfit(db_session, user_id)
    item = (await db_session.scalars(
        select(WardrobeItem).where(WardrobeItem.user_id == user_id, WardrobeItem.category == "bottom")
    )).one()
    cutout_key = f"{user_id}/items/{item.id}/cutout.png"
    cutout_url = f"{base}/files/{cutout_key}"
    crop_key = f"users/{user_id}/wardrobe/crops/recognized.jpg"
    item.cutout_status = CutoutStatus.READY.value
    item.cover_image_url = cutout_url if current else item.original_image_url
    db_session.add_all([
        WardrobeItemImage(
            item_id=item.id, image_type="standard", image_url=cutout_url,
            object_key=cutout_key, created_at=datetime.now(UTC),
        ),
        AsyncJob(
            job_type="cutout", idempotency_key=f"cutout:{item.id}", target_id=item.id, user_id=user_id,
            status=status, result=result, payload={
                "source_key": crop_key if owned else f"users/{uuid.uuid4()}/wardrobe/crops/recognized.jpg",
                "source_url": f"{base}/files/{crop_key}",
            },
        ),
    ])
    await db_session.flush()

    items = await outfit_visual._outfit_board_item_list(db_session, outfit_id=outfit_id, user_id=user_id, generate=True)
    bottom = next(entry for entry in items if entry["role"] == "bottom")
    assert bottom["thumbnail_object_key"] == cutout_key
    assert bottom.get("review_object_key") == (crop_key if uses_crop else None)


@pytest.mark.parametrize("legacy_snapshot", [False, True])
async def test_generated_board_survives_recommendation_replacement(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, legacy_snapshot: bool,
) -> None:
    monkeypatch.setattr(
        outfit_visual, "get_settings",
        lambda: Settings(
            ENABLE_ITEM_CUTOUT=True, IMAGE_CUTOUT_PROVIDER="openai", OPENAI_API_KEY="test", QWEN_API_KEY="test",
        ),
    )

    async def allow(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(outfit_visual, "assert_within_quota", allow)
    user_id = uuid.uuid4()
    outfit_id = await _seed_outfit(db_session, user_id)
    old = await db_session.get(Outfit, outfit_id, options=[selectinload(Outfit.items)])
    assert old is not None
    bottom = next(slot for slot in old.items if slot.role == "bottom")
    bottom_image = await db_session.scalar(select(WardrobeItemImage).where(WardrobeItemImage.item_id == bottom.item_id))
    assert bottom_image is not None
    bottom_image.object_key = f"users/{user_id}/wardrobe/verified-crops/{bottom.item_id}-fixture.jpg"
    await db_session.flush()

    headers = {"X-User-Id": str(user_id)}
    params = {"generate": "true"}
    async with AsyncClient(transport=ASGITransport(app=_app(db_session)), base_url="http://testserver") as client:
        response = await client.post(f"/v1/outfits/{outfit_id}/share-asset", headers=headers, params=params)
        assert response.status_code == 202, response.text
        asset_id = response.json()["asset_id"]
        asset = await db_session.get(OutfitVisualAsset, uuid.UUID(asset_id))
        assert asset and asset.input_snapshot
        if legacy_snapshot:
            asset.input_snapshot = {"generate": True, "item_list": asset.input_snapshot["item_list"]}
        asset.generation_status = "completed"
        asset.image_url = "https://storage.example/verified-board.png"

        replacement = Outfit(
            user_id=user_id, daily_recommendation_id=old.daily_recommendation_id,
            outfit_type=old.outfit_type, title=old.title, reason_text=old.reason_text,
            score_total=old.score_total, rank_order=old.rank_order, created_at=old.created_at,
        )
        slots = [(slot.item_id, slot.role, slot.display_order) for slot in old.items]
        await db_session.delete(old)
        await db_session.flush()
        db_session.add(replacement)
        await db_session.flush()
        replacement_slots = [
            OutfitItem(outfit_id=replacement.id, item_id=item_id, role=role,
                       display_order=order, created_at=replacement.created_at)
            for item_id, role, order in slots
        ]
        db_session.add_all(replacement_slots)
        await db_session.flush()

        path = f"/v1/outfits/{replacement.id}/share-asset"
        lookup = await client.get(path, headers=headers)
        replay = await client.post(path, headers=headers, params=params)
        assert (lookup.status_code, replay.status_code) == (200, 202), (lookup.text, replay.text)
        assert all(result.json()["asset_id"] == asset_id for result in (lookup, replay))
        assert all(result.json()["image_url"] == asset.image_url for result in (lookup, replay))
        assert await db_session.scalar(select(func.count()).select_from(AsyncJob)) == 1

        replacement_slots[-1].role = "outerwear"
        await db_session.flush()
        assert (await client.get(path, headers=headers)).status_code == 404
