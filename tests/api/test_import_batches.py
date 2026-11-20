import uuid
from collections.abc import AsyncIterator
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application import cost_guard
from app.application.import_tasks.batch_groups import GarmentImage
from app.application.import_tasks.import_batch import MATCH_JOB_TYPE
from app.bootstrap import container
from app.config.settings import Settings, get_settings
from app.infrastructure.cache.client import get_redis_client
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.import_task import ImportBatchVerdict, ImportTask
from app.infrastructure.persistence.models.recommendation import RecommendationEvent
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem, WardrobeItemImage
from app.infrastructure.persistence.session import get_db_session
from app.infrastructure.vision.fake_vision import FakeVision
from app.infrastructure.workers import garment_match_worker as match_worker
from app.infrastructure.workers.job_runtime import claim_one
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


@pytest.fixture(autouse=True)
def match_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    _CountingVision.calls = 0
    monkeypatch.setattr(container, "vision_service", _CountingVision)
    monkeypatch.setattr(match_worker, "load_garment_image", _load)


async def run_matching(session: AsyncSession) -> list[AsyncJob]:
    """Drain garment_match jobs on the test connection, like the cron drainer."""
    done = []
    while (job := await claim_one(session, [MATCH_JOB_TYPE])) is not None:
        await match_worker._match_one(session, job)
        done.append(job)
    return done


async def match_jobs(session: AsyncSession) -> list[AsyncJob]:
    return list(
        await session.scalars(
            select(AsyncJob).where(AsyncJob.job_type == MATCH_JOB_TYPE).execution_options(populate_existing=True)
        )
    )


async def settled(client: AsyncClient, session: AsyncSession, user_id: uuid.UUID, batch_id: uuid.UUID) -> Any:
    """Read (schedules comparisons), run the job, read again."""
    await get_batch(client, user_id, batch_id)
    await run_matching(session)
    return await get_batch(client, user_id, batch_id)


def build_app(session: AsyncSession) -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)

    async def override_db_session() -> AsyncSession:
        return session

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = lambda: Settings(APP_ENV="dev", VISION_PROVIDER="fake")
    app.dependency_overrides[get_redis_client] = _Redis
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
        garment("tee2", "top", "cream", "t_shirt", "tee-1", confidence=0.95),
        garment("jeans", "bottom", "light_blue", "jeans", "jeans-2"),
    ], taken_at=DAY_2)
    await seed_photo(db_session, user_id, batch_id, "p3", [], status="processing", taken_at=None)
    await seed_photo(db_session, user_id, batch_id, "p4", [], status="failed", taken_at=None)
    async with AsyncClient(transport=ASGITransport(app=build_app(db_session)), base_url="http://t") as client:
        before = (await get_batch(client, user_id, batch_id)).json()["data"]
        await get_batch(client, user_id, batch_id)
        # Reads never call the model; they queue one comparison job, once.
        assert _CountingVision.calls == 0
        queued = await match_jobs(db_session)
        assert [job.status for job in queued] == ["queued"]
        ran = await run_matching(db_session)
        calls_in_job = _CountingVision.calls
        response = await get_batch(client, user_id, batch_id)
        again = await get_batch(client, user_id, batch_id)

    assert before["complete"] is False and len(before["groups"]) == 4  # nothing compared yet
    assert len(ran) == 1 and ran[0].id == queued[0].id
    data = response.json()["data"]
    assert (data["total_photos"], data["processing"], data["failed"], data["complete"]) == (4, 1, 1, False)
    assert data["comparison_limited"] is False
    groups = group_by_title(data)
    assert set(groups) == {"t_shirt", "jeans"}
    tee = groups["t_shirt"]
    assert tee["kind"] == "new" and tee["status"] == "pending"
    assert [m["task_id"] for m in tee["members"]] == [str(first.id), str(second.id)]
    assert tee["best_crop_url"] == "https://cdn/crops/tee-1/tee2.jpg"
    assert tee["color_main"] == "cream"  # the more confident member represents the group
    assert tee["wear_dates"] == [DAY_1.isoformat(), DAY_2.isoformat()]
    assert tee["members"][0]["photo_url"] == "https://cdn/photos/p1.jpg"
    assert tee["existing_item"] is None
    assert groups["jeans"]["kind"] == "new"
    jeans = [g for g in data["groups"] if g["category"] == "bottom"]
    assert len(jeans) == 2  # different crops → different jeans
    # Verdicts are cached on the batch; nothing is left to compare, so no new job.
    assert calls_in_job == 2 and _CountingVision.calls == 2
    assert again.json()["data"]["groups"] == data["groups"]
    assert await db_session.scalar(select(func.count()).select_from(ImportBatchVerdict)) == 2
    assert len(await match_jobs(db_session)) == 1
    finished = (await match_jobs(db_session))[0]
    assert finished.status == "succeeded" and finished.cost_tokens == 2
    assert finished.result is not None and finished.result["outcome"] == "complete"


async def test_daily_quota_stops_comparisons(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        cost_guard, "get_settings", lambda: Settings(ENABLE_COST_GUARD=True, GARMENT_MATCH_DAILY_CALL_LIMIT=1),
    )
    user_id, batch_id = uuid.uuid4(), uuid.uuid4()
    for index in range(1, 4):
        await seed_photo(db_session, user_id, batch_id, f"p{index}", [
            garment(f"tee{index}", "top", "white", "t_shirt", "tee-1"),
            garment(f"jeans{index}", "bottom", "blue", "jeans", f"jeans-{index}"),
        ])
    async with AsyncClient(transport=ASGITransport(app=build_app(db_session)), base_url="http://t") as client:
        await get_batch(client, user_id, batch_id)
        await run_matching(db_session)
        # The read schedules the remaining pairs; that run finds the quota spent.
        mid = (await get_batch(client, user_id, batch_id)).json()["data"]
        await run_matching(db_session)
        final = (await get_batch(client, user_id, batch_id)).json()["data"]
        repeat = (await get_batch(client, user_id, batch_id)).json()["data"]

    assert mid["complete"] is False
    assert _CountingVision.calls == 1  # the one call the quota allowed
    jobs = await match_jobs(db_session)
    assert [job.result["outcome"] for job in jobs if job.result] == ["quota_limited", "quota_limited"]
    assert sum(job.cost_tokens for job in jobs) == 1
    assert final["complete"] is True and final["comparison_limited"] is True
    # Uncompared photos stay separate groups; nothing more is queued.
    assert len(final["groups"]) > 2
    assert repeat == final and len(await match_jobs(db_session)) == 2


async def test_batch_tasks_stay_out_of_the_pending_list(db_session: AsyncSession) -> None:
    user_id, batch_id = uuid.uuid4(), uuid.uuid4()
    scanned = await seed_photo(db_session, user_id, batch_id, "p1", [garment("tee", "top", "white", "t_shirt", "t")])
    single = ImportTask(
        user_id=user_id, input_type="image", source_type="camera", task_status="waiting_confirm",
        input_payload={"image_url": "https://cdn/photos/single.jpg"}, candidate_payload=None,
    )
    db_session.add(single)
    await db_session.flush()
    headers = {"X-User-Id": str(user_id)}
    async with AsyncClient(transport=ASGITransport(app=build_app(db_session)), base_url="http://t") as client:
        pending = await client.get("/v1/import-tasks", headers=headers)
        by_id = await client.get(f"/v1/import-tasks/{scanned.id}", headers=headers)
    assert [task["task_id"] for task in pending.json()["data"]] == [str(single.id)]
    assert by_id.status_code == 200 and by_id.json()["data"]["task_id"] == str(scanned.id)


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
        response = await settled(client, db_session, user_id, batch_id)

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
        groups = group_by_title((await settled(client, db_session, user_id, batch_id)).json()["data"])
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


async def test_replay_succeeds_after_two_groups_were_linked_to_one_item(db_session: AsyncSession) -> None:
    user_id, batch_id = uuid.uuid4(), uuid.uuid4()
    await seed_photo(db_session, user_id, batch_id, "p1", [garment("tee", "top", "white", "t_shirt", "tee-a")])
    await seed_photo(db_session, user_id, batch_id, "p2", [garment("tee", "top", "white", "t_shirt", "tee-b")])
    owned = await seed_item(db_session, user_id, "white t_shirt", "tee-owned")
    async with AsyncClient(transport=ASGITransport(app=build_app(db_session)), base_url="http://t") as client:
        groups = (await settled(client, db_session, user_id, batch_id)).json()["data"]["groups"]
        assert len(groups) == 2 and {g["existing_item"]["item_id"] for g in groups} == {str(owned.id)}
        decisions = [{"group_id": g["group_id"], "action": "link", "backfill_wear": True} for g in groups]
        first = await _confirm(client, user_id, batch_id, decisions)
        # Both groups now share one item, so the read shows them as one group…
        merged = (await get_batch(client, user_id, batch_id)).json()["data"]["groups"]
        # …and the identical retry still resolves both ids.
        retry = await _confirm(client, user_id, batch_id, decisions)

    assert first.status_code == 200 and len(merged) == 1
    assert retry.status_code == 200, retry.text
    assert [r["group_id"] for r in retry.json()["data"]["groups"]] == [g["group_id"] for g in groups]
    assert {r["item_id"] for r in retry.json()["data"]["groups"]} == {str(owned.id)}
    assert all(r["wears_recorded"] == 0 for r in retry.json()["data"]["groups"])


async def test_deleted_item_can_be_created_again_from_its_batch(db_session: AsyncSession) -> None:
    user_id, batch_id = uuid.uuid4(), uuid.uuid4()
    task = await seed_photo(db_session, user_id, batch_id, "p1", [garment("tee", "top", "white", "t_shirt", "tee-1")])
    async with AsyncClient(transport=ASGITransport(app=build_app(db_session)), base_url="http://t") as client:
        group_id = (await settled(client, db_session, user_id, batch_id)).json()["data"]["groups"][0]["group_id"]
        created = await _confirm(client, user_id, batch_id, [{"group_id": group_id, "action": "create"}])
        first_id = uuid.UUID(created.json()["data"]["groups"][0]["item_id"])
        first_item = await db_session.get(WardrobeItem, first_id)
        assert first_item is not None
        first_item.deleted_at = datetime.now(UTC)
        await db_session.flush()
        reopened = (await get_batch(client, user_id, batch_id)).json()["data"]["groups"][0]
        again = await _confirm(client, user_id, batch_id, [{"group_id": group_id, "action": "create"}])

    assert reopened["status"] == "pending" and reopened["kind"] == "new"
    assert again.status_code == 200, again.text
    result = again.json()["data"]["groups"][0]
    assert result["created"] is True and result["item_id"] != str(first_id)
    await db_session.refresh(task)
    assert task.task_status == "completed"
    assert task.input_payload["confirmed_items"]["tee"] == result["item_id"]
