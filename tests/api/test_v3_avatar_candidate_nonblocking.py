"""V3-012 acceptance 2 — candidate detection cannot break its host flow.

This is the `non-blocking failure test` from `evidence_required`, at the level
where it means something: through the real OOTD and import endpoints, asserting
both that the response is a success **and** that the thing the user actually
asked to save is in the database afterwards. Status code alone would be a weak
assertion — a swallowed exception that left the session rollback-only could
still return 200 while silently discarding the OOTD photo.

The host request never runs detection at all: it enqueues, and
`avatar_candidate_worker` does the work later. So these tests assert the queue
boundary, not a verdict — that is `tests/services/test_v3_avatar_candidate_detection.py`.
"""

from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.avatar import enqueue_candidate_detection as enqueue_module
from app.common.enums import AvatarSourceType
from app.config.settings import Settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.import_task import ImportTask
from app.infrastructure.persistence.models.recommendation import WearHistory
from app.presentation.api.v1.routers import import_tasks as import_tasks_router
from app.presentation.api.v1.routers import outfit_history as history_router
from tests.api.test_import_tasks import build_import_tasks_app
from tests.api.test_outfit_history import build_history_app, seed_wear_history

JOB_TYPE = enqueue_module.JOB_TYPE
OBJECT_KEY = "users/abc/ootd/selfie.jpg"
# Derived from the configured storage base rather than hard-coded, so the
# fixture cannot quietly stop being an internal URL on a host whose
# LOCAL_STORAGE_BASE_URL differs — which would turn every "a job was queued"
# assertion below into a false negative.
INTERNAL_URL = f"{Settings().LOCAL_STORAGE_BASE_URL.rstrip('/')}/files/{OBJECT_KEY}"
EXTERNAL_URL = "https://cdn.example.com/somebody-elses/photo.jpg"


def test_the_fixture_url_really_resolves_to_the_expected_object_key() -> None:
    """Guards the premise of every test in this file: if this fails, the URL is
    no longer recognised as ours and the queue assertions mean nothing."""
    from app.infrastructure.storage.object_key import extract_internal_object_key

    settings = Settings(APP_ENV="dev")
    assert extract_internal_object_key(INTERNAL_URL, settings) == OBJECT_KEY
    assert extract_internal_object_key(EXTERNAL_URL, settings) is None


def _enable_avatar(monkeypatch: pytest.MonkeyPatch, module: object, *, enabled: bool = True) -> None:
    """The flag has to be patched on the *consuming module*: `get_settings` is
    `lru_cache`d, so a FastAPI dependency override does not reach a direct call
    (BE CLAUDE.md, test-infrastructure note)."""

    def _settings() -> Settings:
        return Settings(APP_ENV="dev", VISION_PROVIDER="fake", ENABLE_V3_AVATAR=enabled)

    monkeypatch.setattr(module, "get_settings", _settings)


async def _avatar_jobs(session: AsyncSession) -> list[AsyncJob]:
    return list(
        (await session.scalars(select(AsyncJob).where(AsyncJob.job_type == JOB_TYPE))).all()
    )


# --- source A: the OOTD upload --------------------------------------------


async def test_an_ootd_upload_queues_detection_without_delaying_the_response(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from httpx import ASGITransport, AsyncClient

    _enable_avatar(monkeypatch, history_router)
    user_id = uuid.uuid4()
    history = await seed_wear_history(db_session, user_id, date(2026, 8, 14))

    app = build_history_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            f"/v1/outfit-history/{history.id}/ootd",
            headers={"X-User-Id": str(user_id)},
            json={"ootd_image_url": INTERNAL_URL},
        )

    assert response.status_code == 200
    assert history.ootd_image_url == INTERNAL_URL

    jobs = await _avatar_jobs(db_session)
    assert len(jobs) == 1
    payload = jobs[0].payload or {}
    assert payload["source_type"] == AvatarSourceType.OOTD_PHOTO.value
    # The STABLE key, not the URL the client sent — contract §7 and a stable
    # idempotency key both depend on this.
    assert payload["object_key"] == OBJECT_KEY
    assert "http" not in str(payload)
    assert jobs[0].user_id == user_id


async def test_an_ootd_upload_survives_a_failing_enqueue(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The non-blocking failure test.

    The user asked to save an OOTD photo. A fault in a feature they have not even
    consented to must not cost them that. Without the SAVEPOINT the failed
    avatar insert would mark the request's transaction rollback-only and the
    photo would be lost.
    """
    from httpx import ASGITransport, AsyncClient

    _enable_avatar(monkeypatch, history_router)

    async def _explode(*args: object, **kwargs: object) -> object:
        raise RuntimeError("async_jobs is unhappy")

    monkeypatch.setattr(enqueue_module, "enqueue", _explode)

    user_id = uuid.uuid4()
    history = await seed_wear_history(db_session, user_id, date(2026, 8, 14))

    app = build_history_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            f"/v1/outfit-history/{history.id}/ootd",
            headers={"X-User-Id": str(user_id)},
            json={"ootd_image_url": INTERNAL_URL},
        )

    assert response.status_code == 200
    assert response.json()["ootd_image_url"] == INTERNAL_URL
    # The load-bearing half: the photo is really persisted, and the session is
    # still usable enough to prove it.
    #
    # Read back with a plain awaited column select rather than
    # `expire_all()` + attribute access: expiring an ORM object makes the next
    # attribute read a lazy refresh, which is illegal IO under asyncio (the
    # trap V3-003 recorded). A column select proves the same thing — the value
    # is in the database, and the session still works — without that hazard.
    persisted = await db_session.scalar(
        select(WearHistory.ootd_image_url).where(WearHistory.id == history.id)
    )
    assert persisted == INTERNAL_URL
    assert await _avatar_jobs(db_session) == []


async def test_the_flag_off_queues_nothing(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    from httpx import ASGITransport, AsyncClient

    _enable_avatar(monkeypatch, history_router, enabled=False)
    user_id = uuid.uuid4()
    history = await seed_wear_history(db_session, user_id, date(2026, 8, 14))

    app = build_history_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            f"/v1/outfit-history/{history.id}/ootd",
            headers={"X-User-Id": str(user_id)},
            json={"ootd_image_url": INTERNAL_URL},
        )

    assert response.status_code == 200
    assert history.ootd_image_url == INTERNAL_URL
    assert await _avatar_jobs(db_session) == []


async def test_an_external_photo_url_is_not_queued(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """V3-011 acceptance 2 forbids treating an expiring/foreign URL as durable
    truth, and an external URL is not ours to fetch person material from."""
    from httpx import ASGITransport, AsyncClient

    _enable_avatar(monkeypatch, history_router)
    user_id = uuid.uuid4()
    history = await seed_wear_history(db_session, user_id, date(2026, 8, 14))

    app = build_history_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post(
            f"/v1/outfit-history/{history.id}/ootd",
            headers={"X-User-Id": str(user_id)},
            json={"ootd_image_url": EXTERNAL_URL},
        )

    assert response.status_code == 200
    assert history.ootd_image_url == EXTERNAL_URL
    assert await _avatar_jobs(db_session) == []


async def test_re_uploading_the_same_photo_does_not_queue_a_second_detection(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Prompt-once holding at the queue boundary: the idempotency key is derived
    from the object key, so the second upload is an idempotent hit."""
    from httpx import ASGITransport, AsyncClient

    _enable_avatar(monkeypatch, history_router)
    user_id = uuid.uuid4()
    first = await seed_wear_history(db_session, user_id, date(2026, 8, 14))
    second = await seed_wear_history(db_session, user_id, date(2026, 8, 15))

    app = build_history_app(db_session)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        for history in (first, second):
            response = await client.post(
                f"/v1/outfit-history/{history.id}/ootd",
                headers={"X-User-Id": str(user_id)},
                json={"ootd_image_url": INTERNAL_URL},
            )
            assert response.status_code == 200

    assert len(await _avatar_jobs(db_session)) == 1


# --- source B: the outfit import ------------------------------------------


async def _seed_waiting_outfit_import(
    session: AsyncSession, user_id: uuid.UUID, *, input_type: str, image_url: str
) -> ImportTask:
    from app.common.enums import ImportStatus
    from app.infrastructure.persistence.models.user import User

    if await session.scalar(select(User).where(User.id == user_id)) is None:
        session.add(User(id=user_id))
        await session.flush()
    task = ImportTask(
        user_id=user_id,
        input_type=input_type,
        source_type="album",
        task_status=ImportStatus.WAITING_CONFIRM.value,
        input_payload={"image_url": image_url, "source_url": None, "locale": "zh-CN"},
        candidate_payload={
            "candidate_items": [
                {
                    "temp_item_id": "t1",
                    "title": "Shirt",
                    "category": "top",
                    "color_main": "white",
                    "should_import": True,
                }
            ]
        },
    )
    session.add(task)
    await session.flush()
    return task


async def _confirm_items(app: object, task_id: uuid.UUID, user_id: uuid.UUID) -> int:
    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=app),  # type: ignore[arg-type]
        base_url="http://testserver",
    ) as client:
        response = await client.post(
            f"/v1/import-tasks/{task_id}/confirm-items",
            headers={"X-User-Id": str(user_id)},
            json={
                "items": [
                    {
                        "temp_item_id": "t1",
                        "title": "Shirt",
                        "category": "top",
                        # Required by ConfirmItemInput; omitting it is a 422, not
                        # a detection problem.
                        "color_main": "white",
                    }
                ]
            },
        )
    return response.status_code


async def test_a_confirmed_outfit_import_queues_detection(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Source B, hooked after the garments land: §7.1 requires the person
    question to come after the clothing import, separately authorized."""
    _enable_avatar(monkeypatch, import_tasks_router)
    user_id = uuid.uuid4()
    task = await _seed_waiting_outfit_import(
        db_session, user_id, input_type="outfit_image", image_url=INTERNAL_URL
    )

    assert await _confirm_items(build_import_tasks_app(db_session), task.id, user_id) == 200

    jobs = await _avatar_jobs(db_session)
    assert len(jobs) == 1
    assert (jobs[0].payload or {})["source_type"] == AvatarSourceType.OUTFIT_IMPORT.value
    assert (jobs[0].payload or {})["object_key"] == OBJECT_KEY


async def test_a_single_garment_import_is_not_a_person_photo(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`image` is one garment, not someone wearing an outfit. Running human
    segmentation over it would spend CPU to learn what the input type already
    says — and a rejected row would then hold that object key forever."""
    _enable_avatar(monkeypatch, import_tasks_router)
    user_id = uuid.uuid4()
    task = await _seed_waiting_outfit_import(
        db_session, user_id, input_type="image", image_url=INTERNAL_URL
    )

    assert await _confirm_items(build_import_tasks_app(db_session), task.id, user_id) == 200
    assert await _avatar_jobs(db_session) == []


async def test_an_outfit_import_survives_a_failing_enqueue(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The other half of acceptance 2: the import the user already completed is
    not rolled back by a detection fault."""
    from app.infrastructure.persistence.models.wardrobe import WardrobeItem

    _enable_avatar(monkeypatch, import_tasks_router)

    async def _explode(*args: object, **kwargs: object) -> object:
        raise RuntimeError("async_jobs is unhappy")

    monkeypatch.setattr(enqueue_module, "enqueue", _explode)

    user_id = uuid.uuid4()
    task = await _seed_waiting_outfit_import(
        db_session, user_id, input_type="outfit_image", image_url=INTERNAL_URL
    )

    assert await _confirm_items(build_import_tasks_app(db_session), task.id, user_id) == 200
    assert await _avatar_jobs(db_session) == []
    # The garment the user confirmed really is in their wardrobe.
    items = list((await db_session.scalars(select(WardrobeItem).where(WardrobeItem.user_id == user_id))).all())
    assert len(items) == 1
