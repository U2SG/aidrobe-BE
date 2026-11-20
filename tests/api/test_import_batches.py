import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.import_tasks.batch_groups import GarmentImage
from app.config.settings import Settings, get_settings
from app.infrastructure.cache.client import get_redis_client
from app.infrastructure.persistence.models.import_task import ImportBatchVerdict, ImportTask
from app.infrastructure.persistence.models.recommendation import RecommendationEvent
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem, WardrobeItemImage
from app.infrastructure.persistence.session import get_db_session
from app.infrastructure.vision.fake_vision import FakeVision
from app.presentation.api.v1.routers import import_batches as batches_router
from app.presentation.api.v1.routers import import_tasks as import_tasks_router
from app.presentation.middlewares.exception_handler import setup_exception_handlers

pytestmark = pytest.mark.usefixtures("import_api_environment")

DAY_1, DAY_2 = date(2026, 10, 1), date(2026, 10, 5)


class _Redis:
    async def scan_iter(self, match: str) -> AsyncIterator[str]:
        for key in ():
            yield key

    async def delete(self, *keys: str) -> None:
        return None


class _CountingVision(FakeVision):
    calls = 0

    async def same_garment(self, crop_a: bytes, crop_b: bytes, garment: str) -> bool:
        type(self).calls += 1
        return await super().same_garment(crop_a, crop_b, garment)


async def _load(image: GarmentImage) -> bytes:
    # https://cdn/crops/<garment>/<anything> shows <garment>.
    assert image.url is not None
    return f"garment:{image.url.split('/')[4]} {image.url}".encode()


def build_app(session: AsyncSession) -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)

    async def override_db_session() -> AsyncSession:
        return session

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = lambda: Settings(APP_ENV="dev", VISION_PROVIDER="fake")
    app.dependency_overrides[get_redis_client] = _Redis
    app.dependency_overrides[batches_router.get_vision_service] = _CountingVision
    app.dependency_overrides[batches_router.get_image_loader] = lambda: _load
    app.include_router(import_tasks_router.router)
    app.include_router(batches_router.router)
    return app


def garment(temp: str, category: str, color: str, name: str, crop: str | None, **extra: Any) -> dict[str, Any]:
    return {
        "temp_item_id": temp, "title": name, "display_name": name, "canonical_name": name,
        "category": category, "color_main": color, "confidence_score": extra.pop("confidence", 0.8),
        "original_image_url": "", "should_import": True,
        **({"crop_image_url": f"https://cdn/crops/{crop}/{temp}.jpg"} if crop else {}),
        **extra,
    }


async def seed_photo(
    session: AsyncSession, user_id: uuid.UUID, batch_id: uuid.UUID, name: str,
    candidates: list[dict[str, Any]], *, taken_at: date | None = DAY_1, status: str = "waiting_confirm",
) -> ImportTask:
    if await session.get(User, user_id) is None:
        session.add(User(id=user_id))
        await session.flush()
    url = f"https://cdn/photos/{name}.jpg"
    for candidate in candidates:
        candidate["original_image_url"] = url
    task = ImportTask(
        user_id=user_id, input_type="outfit_image", source_type="album", task_status=status,
        input_payload={"image_url": url, "source_url": None, "locale": None},
        candidate_payload={**candidates[0], "candidate_items": candidates} if candidates else None,
        batch_id=batch_id, taken_at=taken_at, created_at=datetime.now(UTC) + timedelta(seconds=int(name[1:])),
    )
    session.add(task)
    await session.flush()
    return task


async def seed_item(session: AsyncSession, user_id: uuid.UUID, title: str, crop: str, **fields: Any) -> WardrobeItem:
    item = WardrobeItem(
        user_id=user_id, title=title, category=fields.get("category", "top"), color_main=fields.get("color", "white"),
        canonical_name=title, original_image_url="https://cdn/photos/old.jpg",
        cover_image_url="https://cdn/old-cover.png", created_at=datetime.now(UTC),
    )
    session.add(item)
    await session.flush()
    session.add(
        WardrobeItemImage(
            item_id=item.id, image_type="crop", image_url=f"https://cdn/crops/{crop}/item.jpg",
            created_at=datetime.now(UTC),
        )
    )
    await session.flush()
    return item


async def get_batch(client: AsyncClient, user_id: uuid.UUID, batch_id: uuid.UUID) -> Any:
    return await client.get(f"/v1/import-batches/{batch_id}", headers={"X-User-Id": str(user_id)})


def group_by_title(data: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {group["title"]: group for group in data["groups"]}


async def test_batch_groups_garments_across_photos(db_session: AsyncSession) -> None:
    user_id, batch_id = uuid.uuid4(), uuid.uuid4()
    first = await seed_photo(db_session, user_id, batch_id, "p1", [
        garment("tee", "top", "white", "t_shirt", "tee-1"),
        garment("jeans", "bottom", "blue", "jeans", "jeans-1"),
    ], taken_at=DAY_1)
    second = await seed_photo(db_session, user_id, batch_id, "p2", [
        garment("tee", "top", "cream", "t_shirt", "tee-1", confidence=0.95),
        garment("jeans", "bottom", "light_blue", "jeans", "jeans-2"),
    ], taken_at=DAY_2)
    await seed_photo(db_session, user_id, batch_id, "p3", [], status="processing", taken_at=None)
    await seed_photo(db_session, user_id, batch_id, "p4", [], status="failed", taken_at=None)
    _CountingVision.calls = 0
    async with AsyncClient(transport=ASGITransport(app=build_app(db_session)), base_url="http://t") as client:
        response = await get_batch(client, user_id, batch_id)
        calls_after_first = _CountingVision.calls
        again = await get_batch(client, user_id, batch_id)

    data = response.json()["data"]
    assert (data["total_photos"], data["processing"], data["failed"], data["complete"]) == (4, 1, 1, False)
    groups = group_by_title(data)
    assert set(groups) == {"t_shirt", "jeans"}
    tee = groups["t_shirt"]
    assert tee["kind"] == "new" and tee["status"] == "pending"
    assert [m["task_id"] for m in tee["members"]] == [str(first.id), str(second.id)]
    assert tee["best_crop_url"] == "https://cdn/crops/tee-1/tee.jpg"
    assert tee["color_main"] == "cream"  # the more confident member represents the group
    assert tee["wear_dates"] == [DAY_1.isoformat(), DAY_2.isoformat()]
    assert tee["members"][0]["photo_url"] == "https://cdn/photos/p1.jpg"
    assert tee["existing_item"] is None
    assert groups["jeans"]["kind"] == "new"
    jeans = [g for g in data["groups"] if g["category"] == "bottom"]
    assert len(jeans) == 2  # different crops → different jeans
    # Verdicts are cached on the batch: a second read asks vision nothing.
    assert calls_after_first == 1
    assert _CountingVision.calls == calls_after_first
    assert again.json()["data"]["groups"] == data["groups"]
    assert await db_session.scalar(select(func.count()).select_from(ImportBatchVerdict)) == 1


async def test_batch_matches_existing_items(db_session: AsyncSession) -> None:
    user_id, batch_id = uuid.uuid4(), uuid.uuid4()
    await seed_photo(db_session, user_id, batch_id, "p1", [
        garment("tee", "top", "white", "t_shirt", "tee-1"),
        garment("shirt", "top", "white", "shirt", "shirt-1"),
        garment("inner", "top", "white", "tank_top", "tank-1", visibility="partially_hidden"),
        garment("watch", "accessory", "black", "watch", None),
    ])
    same = await seed_item(db_session, user_id, "white t_shirt", "tee-1")
    lookalike = await seed_item(db_session, user_id, "white shirt", "other-shirt")
    async with AsyncClient(transport=ASGITransport(app=build_app(db_session)), base_url="http://t") as client:
        response = await get_batch(client, user_id, batch_id)

    groups = group_by_title(response.json()["data"])
    assert groups["t_shirt"]["kind"] == "existing"
    assert groups["t_shirt"]["existing_item"]["item_id"] == str(same.id)
    assert groups["shirt"]["kind"] == "uncertain"
    assert groups["shirt"]["existing_item"]["item_id"] == str(lookalike.id)
    assert groups["tank_top"]["kind"] == "uncertain"  # hidden layer: never verified
    assert groups["watch"]["kind"] == "uncertain"
    assert groups["watch"]["best_crop_url"] is None
    assert response.json()["data"]["complete"] is True


async def test_foreign_or_unknown_batch_is_404(db_session: AsyncSession) -> None:
    owner, other, batch_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await seed_photo(db_session, owner, batch_id, "p1", [garment("tee", "top", "white", "t_shirt", "tee-1")])
    db_session.add(User(id=other))
    await db_session.flush()
    async with AsyncClient(transport=ASGITransport(app=build_app(db_session)), base_url="http://t") as client:
        foreign = await get_batch(client, other, batch_id)
        confirm = await client.post(
            f"/v1/import-batches/{batch_id}/confirm", headers={"X-User-Id": str(other)},
            json={"groups": [{"group_id": str(uuid.uuid4()), "action": "skip"}]},
        )
    assert foreign.status_code == 404
    assert confirm.status_code == 404


async def test_create_task_records_batch_and_rejects_future_capture_date(db_session: AsyncSession) -> None:
    user_id, batch_id = uuid.uuid4(), uuid.uuid4()
    db_session.add(User(id=user_id))
    await db_session.flush()
    body = {"input_type": "outfit_image", "source_type": "album", "image_url": "https://cdn/photos/a.jpg",
            "batch_id": str(batch_id)}
    async with AsyncClient(transport=ASGITransport(app=build_app(db_session)), base_url="http://t") as client:
        future = await client.post("/v1/import-tasks", headers={"X-User-Id": str(user_id)},
                                   json={**body, "taken_at": (date.today() + timedelta(days=2)).isoformat()})
        created = await client.post("/v1/import-tasks", headers={"X-User-Id": str(user_id)},
                                    json={**body, "taken_at": DAY_1.isoformat()})
    assert future.status_code == 422
    assert created.status_code == 202
    task = await db_session.get(ImportTask, uuid.UUID(created.json()["data"]["task_id"]))
    assert task is not None
    assert (task.batch_id, task.taken_at) == (batch_id, DAY_1)


async def _confirm(client: AsyncClient, user_id: uuid.UUID, batch_id: uuid.UUID, groups: list[dict[str, Any]]) -> Any:
    return await client.post(
        f"/v1/import-batches/{batch_id}/confirm", headers={"X-User-Id": str(user_id)}, json={"groups": groups},
    )


async def _wears(session: AsyncSession, item_id: uuid.UUID) -> list[str]:
    rows = await session.scalars(select(RecommendationEvent).where(RecommendationEvent.item_id == item_id))
    return sorted(row.event_metadata["worn_date"] for row in rows)


async def test_confirm_create_link_skip_is_idempotent(db_session: AsyncSession) -> None:
    user_id, batch_id = uuid.uuid4(), uuid.uuid4()
    first = await seed_photo(db_session, user_id, batch_id, "p1", [
        garment("tee", "top", "white", "t_shirt", "tee-1"),
        garment("jeans", "bottom", "blue", "jeans", "jeans-1"),
        garment("cap", "accessory", "red", "cap", "cap-1"),
    ], taken_at=DAY_1)
    second = await seed_photo(db_session, user_id, batch_id, "p2", [
        garment("tee", "top", "white", "t_shirt", "tee-1", confidence=0.95),
        garment("jeans", "bottom", "blue", "jeans", "jeans-1"),
    ], taken_at=DAY_2)
    owned_jeans = await seed_item(db_session, user_id, "blue jeans", "jeans-1", category="bottom", color="blue")
    app = build_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        groups = group_by_title((await get_batch(client, user_id, batch_id)).json()["data"])
        assert groups["jeans"]["kind"] == "existing"
        decisions = [
            {"group_id": groups["t_shirt"]["group_id"], "action": "create", "title": "White tee",
             "backfill_wear": True},
            {"group_id": groups["jeans"]["group_id"], "action": "link", "backfill_wear": True},
            {"group_id": groups["cap"]["group_id"], "action": "skip"},
        ]
        response = await _confirm(client, user_id, batch_id, decisions)
        retry = await _confirm(client, user_id, batch_id, decisions)
        after = group_by_title((await get_batch(client, user_id, batch_id)).json()["data"])

    assert response.status_code == 200, response.text
    results = {r["action"]: r for r in response.json()["data"]["groups"]}
    created_id = uuid.UUID(results["create"]["item_id"])
    assert results["create"]["created"] is True
    assert results["create"]["wears_recorded"] == 2
    assert results["link"]["item_id"] == str(owned_jeans.id)
    assert results["link"]["wears_recorded"] == 2
    assert results["skip"]["item_id"] is None

    retry_results = {r["action"]: r for r in retry.json()["data"]["groups"]}
    assert retry_results["create"]["item_id"] == str(created_id)
    assert retry_results["create"]["created"] is False
    assert retry_results["create"]["wears_recorded"] == retry_results["link"]["wears_recorded"] == 0

    items = list(await db_session.scalars(select(WardrobeItem).where(WardrobeItem.user_id == user_id)))
    assert len(items) == 2  # one new tee + the existing jeans
    created = next(item for item in items if item.id == created_id)
    assert created.title == "White tee"
    assert created.wear_count == 2
    assert created.cover_image_url == "https://cdn/crops/tee-1/tee.jpg"  # best member's crop
    assert await _wears(db_session, created_id) == [DAY_1.isoformat(), DAY_2.isoformat()]
    assert await _wears(db_session, owned_jeans.id) == [DAY_1.isoformat(), DAY_2.isoformat()]

    for task in (first, second):
        await db_session.refresh(task)
        assert task.task_status == "completed"
        assert task.input_payload["confirmed_items"]["tee"] == str(created_id)
        assert task.input_payload["confirmed_items"]["jeans"] == str(owned_jeans.id)
    assert first.input_payload["dismissed_items"] == ["cap"]

    assert after["t_shirt"]["status"] == "confirmed"
    assert after["t_shirt"]["existing_item"]["item_id"] == str(created_id)
    assert after["cap"]["status"] == "skipped"


async def test_confirm_rejects_unknown_group_and_link_without_item(db_session: AsyncSession) -> None:
    user_id, batch_id = uuid.uuid4(), uuid.uuid4()
    await seed_photo(db_session, user_id, batch_id, "p1", [garment("tee", "top", "white", "t_shirt", "tee-1")])
    async with AsyncClient(transport=ASGITransport(app=build_app(db_session)), base_url="http://t") as client:
        group_id = (await get_batch(client, user_id, batch_id)).json()["data"]["groups"][0]["group_id"]
        unknown = await _confirm(client, user_id, batch_id, [{"group_id": str(uuid.uuid4()), "action": "skip"}])
        link = await _confirm(client, user_id, batch_id, [{"group_id": group_id, "action": "link"}])
    assert unknown.status_code == 409
    assert link.status_code == 422
