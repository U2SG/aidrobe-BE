"""Photos attached in Ask: conversation linking, note prefill, price/material confirm."""

import json
import uuid
from decimal import Decimal
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports.llm import LlmAnswer
from app.bootstrap.container import llm_service
from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.ask_conversation import AskConversation
from app.infrastructure.persistence.models.import_task import ImportTask
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import ask as ask_router
from app.presentation.api.v1.routers import import_tasks as import_tasks_router
from app.presentation.middlewares.exception_handler import setup_exception_handlers

pytestmark = pytest.mark.usefixtures("import_api_environment")


class FakeLlm:
    def __init__(self, reply: str | Exception) -> None:
        self.reply = reply
        self.calls: list[tuple[str, str]] = []

    async def polish_reason(self, prompt: str) -> str:
        return prompt

    async def ask(self, system_prompt: str, question: str, *, json_mode: bool = False) -> LlmAnswer:
        self.calls.append((system_prompt, question))
        if isinstance(self.reply, Exception):
            raise self.reply
        return LlmAnswer(text=self.reply, tokens_used=10)


def build_app(session: AsyncSession, llm: FakeLlm | None = None) -> FastAPI:
    app = FastAPI()
    setup_exception_handlers(app)

    async def override_db_session() -> AsyncSession:
        return session

    app.dependency_overrides[get_db_session] = override_db_session
    app.dependency_overrides[get_settings] = lambda: Settings(APP_ENV="dev", VISION_PROVIDER="fake")
    if llm is not None:
        app.dependency_overrides[llm_service] = lambda: llm
    app.include_router(import_tasks_router.router)
    app.include_router(ask_router.router)
    return app


def _candidate(temp_id: str, title: str, category: str, color: str, **extra: Any) -> dict[str, Any]:
    return {
        "temp_item_id": temp_id, "title": title, "display_name": title, "category": category,
        "color_main": color, "confidence_score": 0.9, "original_image_url": "https://cdn/outfit.jpg",
        "should_import": True, "material": None, **extra,
    }


async def seed_task(session: AsyncSession, user_id: uuid.UUID) -> ImportTask:
    session.add(User(id=user_id))
    await session.flush()
    task = ImportTask(
        user_id=user_id, input_type="image_batch", source_type="album", task_status="waiting_confirm",
        input_payload={"image_url": "https://cdn/outfit.jpg"},
        candidate_payload={"candidate_items": [
            _candidate("tmp_coat", "灰色大衣", "outerwear", "gray"),
            _candidate("tmp_jeans", "蓝色牛仔裤", "bottom", "blue", material="denim"),
        ]},
    )
    session.add(task)
    await session.flush()
    return task


def _by_id(body: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {candidate["temp_item_id"]: candidate for candidate in body["data"]["candidate_items"]}


async def test_create_links_own_conversation_and_rejects_foreign(db_session: AsyncSession) -> None:
    owner, stranger = User(id=uuid.uuid4()), User(id=uuid.uuid4())
    db_session.add_all([owner, stranger])
    await db_session.flush()
    conversation = AskConversation(user_id=owner.id)
    db_session.add(conversation)
    await db_session.flush()
    body = {"input_type": "image", "source_type": "album", "image_url": "https://cdn/shirt.jpg",
            "conversation_id": str(conversation.id)}
    async with AsyncClient(transport=ASGITransport(app=build_app(db_session)), base_url="http://t") as client:
        foreign = await client.post("/v1/import-tasks", headers={"X-User-Id": str(stranger.id)}, json=body)
        own = await client.post("/v1/import-tasks", headers={"X-User-Id": str(owner.id)}, json=body)

    assert foreign.status_code == 404
    assert own.status_code == 202, own.text
    task = await db_session.get(ImportTask, uuid.UUID(own.json()["data"]["task_id"]))
    assert task is not None and task.conversation_id == conversation.id


async def test_conversation_returns_linked_import_tasks(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    user_id = uuid.uuid4()
    task = await seed_task(db_session, user_id)
    conversation = AskConversation(user_id=user_id)
    db_session.add(conversation)
    await db_session.flush()
    task.conversation_id = conversation.id
    db_session.add(ImportTask(  # unlinked: must not appear
        user_id=user_id, input_type="image", source_type="album", task_status="queued", input_payload={},
    ))
    await db_session.flush()
    monkeypatch.setattr(ask_router, "get_settings", lambda: Settings(APP_ENV="dev", ENABLE_ASK_AIDROBE=True))
    async with AsyncClient(transport=ASGITransport(app=build_app(db_session)), base_url="http://t") as client:
        response = await client.get(
            f"/v1/ask/conversations/{conversation.id}", headers={"X-User-Id": str(user_id)},
        )

    assert response.status_code == 200, response.text
    tasks = response.json()["import_tasks"]
    assert [t["task_id"] for t in tasks] == [str(task.id)]
    assert {c["temp_item_id"] for c in tasks[0]["candidate_items"]} == {"tmp_coat", "tmp_jeans"}
    assert tasks[0]["candidate_items"][0]["possible_duplicates"] == []


async def test_apply_note_prefills_excludes_and_ignores_bad_output(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    task = await seed_task(db_session, user_id)
    llm = FakeLlm(json.dumps({"items": [
        {"item": 1, "include": True, "title": "羊毛大衣", "purchase_price": "399", "material": "Wool"},
        {"item": 2, "include": False, "title": "", "purchase_price": -5, "material": "unobtanium"},
        {"item": 9, "include": False},
        {"item": True, "include": False},
        "junk",
    ]}))
    url = f"/v1/import-tasks/{task.id}/apply-note"
    async with AsyncClient(transport=ASGITransport(app=build_app(db_session, llm)), base_url="http://t") as client:
        response = await client.post(url, headers={"X-User-Id": str(user_id)}, json={"note": "只要大衣，399买的，羊毛"})

    assert response.status_code == 200, response.text
    candidates = _by_id(response.json())
    coat, jeans = candidates["tmp_coat"], candidates["tmp_jeans"]
    assert (coat["should_import"], coat["title"], coat["display_name"]) == (True, "羊毛大衣", "羊毛大衣")
    assert (coat["purchase_price"], coat["material"]) == ("399.00", "wool")
    assert jeans["should_import"] is False
    assert (jeans["title"], jeans["purchase_price"], jeans["material"]) == ("蓝色牛仔裤", None, "denim")
    system_prompt, question = llm.calls[0]
    assert question == "只要大衣，399买的，羊毛"
    assert str(user_id) not in system_prompt and "tmp_coat" not in system_prompt
    assert await db_session.scalar(select(WardrobeItem.id)) is None


async def test_reapplying_a_note_starts_from_recognition(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    task = await seed_task(db_session, user_id)
    first = FakeLlm(json.dumps({"items": [{"item": 1, "title": "羊毛大衣", "purchase_price": 399, "material": "wool"},
                                          {"item": 2, "include": False}]}))
    second = FakeLlm(json.dumps({"items": [{"item": 2, "purchase_price": 199.5}]}))
    url, headers = f"/v1/import-tasks/{task.id}/apply-note", {"X-User-Id": str(user_id)}
    async with AsyncClient(transport=ASGITransport(app=build_app(db_session, first)), base_url="http://t") as client:
        await client.post(url, headers=headers, json={"note": "大衣399羊毛，不要裤子"})
    async with AsyncClient(transport=ASGITransport(app=build_app(db_session, second)), base_url="http://t") as client:
        response = await client.post(url, headers=headers, json={"note": "裤子199.5"})
        # The second note is read against the recognised titles, not the first note's.
        assert "灰色大衣" in second.calls[0][0] and "羊毛大衣" not in second.calls[0][0]

    candidates = _by_id(response.json())
    coat, jeans = candidates["tmp_coat"], candidates["tmp_jeans"]
    assert (coat["title"], coat["purchase_price"], coat["material"]) == ("灰色大衣", None, None)
    assert (jeans["should_import"], jeans["purchase_price"]) == (True, "199.50")


@pytest.mark.parametrize("reply", ["", "not json", RuntimeError("timeout")])
async def test_apply_note_llm_failure_leaves_payload_unchanged(
    db_session: AsyncSession, reply: str | Exception,
) -> None:
    user_id = uuid.uuid4()
    task = await seed_task(db_session, user_id)
    before = json.loads(json.dumps(task.candidate_payload))
    url = f"/v1/import-tasks/{task.id}/apply-note"
    app = build_app(db_session, FakeLlm(reply))
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        response = await client.post(url, headers={"X-User-Id": str(user_id)}, json={"note": "只要大衣"})

    assert response.status_code == 503
    await db_session.refresh(task)
    assert task.candidate_payload == before


async def test_apply_note_requires_waiting_task_and_owner(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    task = await seed_task(db_session, user_id)
    task.task_status = "completed"
    await db_session.flush()
    llm = FakeLlm("{}")
    url = f"/v1/import-tasks/{task.id}/apply-note"
    async with AsyncClient(transport=ASGITransport(app=build_app(db_session, llm)), base_url="http://t") as client:
        done = await client.post(url, headers={"X-User-Id": str(user_id)}, json={"note": "只要大衣"})
        foreign = await client.post(url, headers={"X-User-Id": str(uuid.uuid4())}, json={"note": "只要大衣"})
        empty = await client.post(url, headers={"X-User-Id": str(user_id)}, json={"note": ""})

    assert (done.status_code, foreign.status_code, empty.status_code) == (409, 404, 422)
    assert llm.calls == []


async def test_confirm_writes_price_and_material_with_candidate_fallback(db_session: AsyncSession) -> None:
    user_id = uuid.uuid4()
    task = await seed_task(db_session, user_id)
    llm = FakeLlm(json.dumps({"items": [{"item": 2, "purchase_price": "259", "material": "cotton"}]}))
    headers = {"X-User-Id": str(user_id)}
    async with AsyncClient(transport=ASGITransport(app=build_app(db_session, llm)), base_url="http://t") as client:
        await client.post(f"/v1/import-tasks/{task.id}/apply-note", headers=headers, json={"note": "裤子259"})
        response = await client.post(f"/v1/import-tasks/{task.id}/confirm-items", headers=headers, json={"items": [
            {"temp_item_id": "tmp_coat", "title": "灰色大衣", "category": "outerwear", "color_main": "gray",
             "purchase_price": "399.9", "material": "wool"},
            {"temp_item_id": "tmp_jeans", "title": "蓝色牛仔裤", "category": "bottom", "color_main": "blue"},
        ]})
        invalid = await client.post(f"/v1/import-tasks/{task.id}/confirm-items", headers=headers, json={"items": [
            {"temp_item_id": "tmp_coat", "title": "x", "category": "top", "color_main": "x", "purchase_price": -1},
        ]})

    assert response.status_code == 200, response.text
    assert invalid.status_code == 422
    ids = {entry["temp_item_id"]: uuid.UUID(entry["item_id"]) for entry in response.json()["data"]["created_items"]}
    coat = await db_session.get(WardrobeItem, ids["tmp_coat"])
    jeans = await db_session.get(WardrobeItem, ids["tmp_jeans"])
    assert coat is not None and jeans is not None
    assert (coat.purchase_price, coat.material) == (Decimal("399.90"), "wool")
    assert (jeans.purchase_price, jeans.material) == (Decimal("259.00"), "cotton")
    # The title was the recognised one, so the AI naming provenance stays.
    assert jeans.name_source == "ai"


async def test_apply_note_respects_and_records_the_text_model_quota(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.infrastructure.persistence.models.async_job import AsyncJob

    user_id = uuid.uuid4()
    task = await seed_task(db_session, user_id)
    llm = FakeLlm(json.dumps({"items": [{"item": 1, "include": True}]}))
    url = f"/v1/import-tasks/{task.id}/apply-note"
    headers = {"X-User-Id": str(user_id)}
    app = build_app(db_session, llm)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        allowed = await client.post(url, headers=headers, json={"note": "只要大衣"})
    recorded = (await db_session.scalars(select(AsyncJob).where(AsyncJob.user_id == user_id))).all()
    assert allowed.status_code == 200, allowed.text
    assert [(job.job_type, job.cost_tokens) for job in recorded] == [("llm_ask", 10)]

    exhausted = Settings(APP_ENV="dev", ENABLE_COST_GUARD=True, LLM_DAILY_TOKEN_LIMIT=10)
    monkeypatch.setattr("app.application.cost_guard.get_settings", lambda: exhausted)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as client:
        refused = await client.post(url, headers=headers, json={"note": "只要大衣"})
    assert refused.status_code == 429, refused.text
    assert len(llm.calls) == 1
