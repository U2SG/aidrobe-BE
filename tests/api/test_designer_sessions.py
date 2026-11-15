"""The same persisted outfit reaches Ask, Today, resume and existing actions."""

import fnmatch
import json
import uuid
from collections.abc import AsyncIterator, Sequence
from datetime import date, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports.llm import LlmAnswer, ToolAskAnswer, ToolExecutor, ToolSpec
from app.application.preferences.memory import read_preference_memory, record_preference
from app.bootstrap.container import llm_service
from app.config.settings import Settings, get_settings
from app.infrastructure.cache.client import get_redis_client
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.outfit_visual import OutfitVisualAsset
from app.infrastructure.persistence.models.user import User, UserSettings
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.preference_memory_repo import SqlAlchemyPreferenceMemoryRepository
from app.infrastructure.persistence.repositories.recommendation_repo import SqlAlchemyRecommendationRepository
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import ask, outfit_visual, recommendations
from app.presentation.middlewares.exception_handler import setup_exception_handlers


class _Redis:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    async def get(self, key: str) -> str | None:
        return self.values.get(key)

    async def set(self, key: str, value: str, **_: Any) -> None:
        self.values[key] = value

    async def scan_iter(self, match: str) -> AsyncIterator[str]:
        for key in list(self.values):
            if fnmatch.fnmatch(key, match):
                yield key

    async def delete(self, *keys: str) -> None:
        for key in keys:
            self.values.pop(key, None)


class _Designer:
    def __init__(self) -> None:
        self.calls = 0
        self.change_shoes = False
        self.change_unrequested_top = False
        self.explore = False
        self.preferences_after_update: dict[str, Any] = {}

    async def polish_reason(self, prompt: str) -> str:
        return prompt

    async def ask(self, system_prompt: str, question: str, *, json_mode: bool = False) -> LlmAnswer:
        self.calls += 1
        return LlmAnswer('{"answer":"普通问答","referenced_items":[]}', 3)

    async def ask_with_tools(
        self, system_prompt: str, question: str, tools: Sequence[ToolSpec], execute: ToolExecutor,
        *, json_mode: bool = False, max_rounds: int = 4,
    ) -> ToolAskAnswer:
        self.calls += 1
        assert "update_design_brief" in {tool.name for tool in tools}
        payload = next(json.loads(line) for line in system_prompt.splitlines() if line.startswith('{"context":'))
        known = payload["items"]

        def position(category: str, color: str) -> int:
            return next(index for index, item in enumerate(known, 1)
                        if item["category"] == category and item["color_main"] == color)

        brief: dict[str, Any] = {"evidence_text": question, "goal": question}
        if self.change_shoes:
            brief["change_positions"] = [position("shoes", "black")]
        if self.explore:
            brief.update(
                exploration=True, target_style="streetwear", preference_overrides=[{"type": "color", "value": "red"}]
            )
        result = await execute("update_design_brief", brief)
        assert result.content["updated"] is True
        self.preferences_after_update = result.content["preferences"]
        top = "red" if self.explore or self.change_unrequested_top else "white"
        shoes = "brown" if self.change_shoes else "black"
        selected = [position("top", top), position("bottom", "black"), position("shoes", shoes)]
        validation = await execute("validate_outfit", {"positions": selected})
        assert validation.content["valid"] is (not self.change_unrequested_top)
        return ToolAskAnswer(json.dumps({
            "answer": "穿这套衬衫、长裤和鞋，保留你想要的细节。", "reply_kind": "outfit", "referenced_items": selected,
        }, ensure_ascii=False), 19)


async def _setup(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, *, enabled: bool = True,
) -> tuple[FastAPI, uuid.UUID, list[WardrobeItem], _Designer, Settings]:
    settings = Settings(APP_ENV="dev", ENABLE_ASK_AIDROBE=True, ENABLE_ASK_TOOLS=enabled,
                        ENABLE_DESIGNER_SESSIONS=enabled, ENABLE_PREFERENCE_MEMORY=True)
    for module in (ask, ask.ask_uc, recommendations):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    user_id = uuid.uuid4()
    session.add(User(id=user_id))
    await session.flush()
    session.add(UserSettings(user_id=user_id, timezone="UTC"))
    items = [WardrobeItem(user_id=user_id, category=category, color_main=color, title=f"{color} {category}")
             for category, color in (("top", "white"), ("bottom", "black"), ("shoes", "black"),
                                     ("shoes", "brown"), ("top", "red"))]
    session.add_all(items)
    await session.flush()
    model = _Designer()
    app = FastAPI()
    setup_exception_handlers(app)
    app.dependency_overrides[get_db_session] = lambda: session
    app.dependency_overrides[get_settings] = lambda: settings
    app.dependency_overrides[get_redis_client] = lambda: cache
    app.dependency_overrides[llm_service] = lambda: model
    app.dependency_overrides[ask._ask_weather_service] = lambda: None
    cache = _Redis()
    app.include_router(ask.router)
    app.include_router(recommendations.router)
    app.include_router(outfit_visual.router)
    return app, user_id, items, model, settings


async def test_design_resume_revision_adoption_and_actions_use_one_outfit(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, user_id, items, model, _ = await _setup(db_session, monkeypatch)
    headers = {"X-User-Id": str(user_id)}
    request = {"question": "设计一套明快的搭配", "design_mode": True, "request_id": str(uuid.uuid4())}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver", headers=headers) as client:
        response = await client.post("/v1/ask", json=request)
        assert response.status_code == 200, response.text
        turn = response.json()
        original = turn["styling"]
        assert original["status"] == "draft" and original["revision"] == 1
        assert turn["recommendation_id"] == turn["ask_session_id"]
        assert {entry["item_id"] for entry in original["outfit"]["items"]} == {str(item.id) for item in items[:3]}
        assert await SqlAlchemyRecommendationRepository(db_session).get_daily(user_id, date.today()) is None
        replay = await client.post("/v1/ask", json=request)
        assert replay.json()["ask_session_id"] == turn["ask_session_id"] and model.calls == 1
        assert await db_session.scalar(select(func.count()).select_from(AsyncJob)) == 1

        conversation_id = original["conversation_id"]
        revision_request = {"request_id": str(uuid.uuid4()), "expected_revision": 1,
                            "replace": {"item_id": str(items[2].id), "replacement_item_id": str(items[3].id)}}
        revised = await client.post(f"/v1/ask/conversations/{conversation_id}/outfit-revisions", json=revision_request)
        assert revised.status_code == 200, revised.text
        current = revised.json()
        assert current["revision"] == 2 and current["status"] == "draft"
        assert current["outfit"]["outfit_id"] != original["outfit"]["outfit_id"]
        assert {entry["item_id"] for entry in current["outfit"]["items"]} == {str(items[i].id) for i in (0, 1, 3)}
        history = (await client.get("/v1/ask/history")).json()
        assert history[0]["styling"]["outfit"]["outfit_id"] == original["outfit"]["outfit_id"]
        recovered = (await client.get(f"/v1/ask/conversations/{conversation_id}")).json()
        assert recovered["styling"] == current
        assistant = next(message for message in recovered["messages"] if message["role"] == "assistant")
        assert assistant["ask_session_id"] == turn["ask_session_id"] and assistant["id"] != turn["ask_session_id"]
        assert assistant["styling"]["revision"] == 1

        adopted = await client.post(f"/v1/ask/conversations/{conversation_id}/adopt", json={
            "request_id": str(uuid.uuid4()), "expected_revision": 2,
        })
        assert adopted.status_code == 200, adopted.text
        adopted_body = adopted.json()
        active_id = current["outfit"]["outfit_id"]
        assert adopted_body["outfit"]["outfit_id"] == active_id and adopted_body["status"] == "adopted"
        today = (await client.get("/v1/recommendations/today")).json()["data"]
        assert today["recommendation_id"] == adopted_body["recommendation_id"]
        assert today["outfits"][0]["outfit_id"] == active_id
        assert (await client.post(f"/v1/recommendations/outfits/{active_id}/favorite")).status_code == 200
        board = await client.post(f"/v1/outfits/{active_id}/share-asset")
        assert board.status_code == 202, board.text
        asset = await db_session.get(OutfitVisualAsset, uuid.UUID(board.json()["asset_id"]))
        assert asset is not None and str(asset.outfit_id) == active_id

        swapped = await client.post(f"/v1/recommendations/outfits/{active_id}/revisions", json={
            "request_id": str(uuid.uuid4()), "item_id": str(items[3].id), "replacement_item_id": str(items[2].id),
        })
        assert swapped.status_code == 200, swapped.text
        after = swapped.json()
        today_after = (await client.get("/v1/recommendations/today")).json()["data"]
        assert today_after["outfits"][0]["outfit_id"] == after["outfit"]["outfit_id"]
        stale = await client.post(f"/v1/recommendations/outfits/{active_id}/revisions", json={
            "request_id": str(uuid.uuid4()), "item_id": str(items[3].id), "replacement_item_id": str(items[2].id),
        })
        assert stale.status_code == 409


async def test_natural_language_partial_edit_cannot_change_unrequested_slots(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, user_id, items, model, _ = await _setup(db_session, monkeypatch)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver",
                           headers={"X-User-Id": str(user_id)}) as client:
        first = (await client.post("/v1/ask", json={
            "question": "给我一套搭配", "design_mode": True, "request_id": str(uuid.uuid4()),
        })).json()
        model.change_shoes = True
        model.change_unrequested_top = True
        rejected = await client.post("/v1/ask", json={
            "question": "只换鞋，其他保持", "design_mode": True, "request_id": str(uuid.uuid4()),
            "conversation_id": first["conversation_id"], "expected_revision": 1,
        })
        assert rejected.status_code == 200
        assert rejected.json()["styling"]["revision"] == 1
        assert rejected.json()["item_refs"] == []
        model.change_unrequested_top = False
        accepted = await client.post("/v1/ask", json={
            "question": "只换鞋，其他保持", "design_mode": True, "request_id": str(uuid.uuid4()),
            "conversation_id": first["conversation_id"], "expected_revision": 1,
        })
        assert accepted.status_code == 200, accepted.text
        latest = accepted.json()["styling"]
        assert latest["revision"] == 2
        assert {entry["item_id"] for entry in latest["outfit"]["items"]} == {str(items[i].id) for i in (0, 1, 3)}


async def test_exploration_exception_applies_this_turn_without_rewriting_memory(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, user_id, items, model, _ = await _setup(db_session, monkeypatch)
    repository = SqlAlchemyPreferenceMemoryRepository(db_session)
    await record_preference(repository, user_id=user_id, preference_type="color", preference_value="red",
                            polarity="negative", confidence=0.9, source="explicit_user_statement")
    model.explore = True
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        response = await client.post("/v1/ask", headers={"X-User-Id": str(user_id)}, json={
            "question": "今天试试红色街头风", "design_mode": True, "request_id": str(uuid.uuid4()),
        })
    assert response.status_code == 200, response.text
    styling = response.json()["styling"]
    assert styling["constraints"]["exploration"] is True
    assert str(items[4].id) in {entry["item_id"] for entry in styling["outfit"]["items"]}
    assert model.preferences_after_update["negative"] == []
    assert [entry["value"] for entry in (await read_preference_memory(repository, user_id))["negative"]] == ["red"]


async def test_flag_off_degrades_new_ask_but_rejects_design_continuation(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, user_id, _, _, _ = await _setup(db_session, monkeypatch, enabled=False)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver",
                           headers={"X-User-Id": str(user_id)}) as client:
        plain = await client.post("/v1/ask", json={
            "question": "hello", "design_mode": True, "request_id": str(uuid.uuid4()),
        })
        assert plain.status_code == 200 and plain.json()["styling"] is None
        continuation = await client.post("/v1/ask", json={
            "question": "换一件", "design_mode": True, "request_id": str(uuid.uuid4()), "expected_revision": 1,
        })
        assert continuation.status_code == 503


async def test_base_outfit_executes_the_first_question_and_inherits_tomorrow(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, user_id, items, model, _ = await _setup(db_session, monkeypatch)
    tomorrow = date.today() + timedelta(days=1)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver",
                           headers={"X-User-Id": str(user_id)}) as client:
        first = (await client.post("/v1/ask", json={
            "question": "明天穿这套", "context": {"date": tomorrow.isoformat()},
            "design_mode": True, "request_id": str(uuid.uuid4()),
        })).json()
        base_id = first["styling"]["outfit"]["outfit_id"]
        adopted = await client.post(f"/v1/ask/conversations/{first['conversation_id']}/adopt", json={
            "request_id": str(uuid.uuid4()), "expected_revision": 1,
        })
        assert adopted.status_code == 200, adopted.text
        assert adopted.json()["recommendation_date"] == tomorrow.isoformat()
        model.change_shoes = True
        changed = await client.post("/v1/ask", json={
            "question": "只换鞋，其他保持", "design_mode": True, "request_id": str(uuid.uuid4()),
            "base_outfit_id": base_id,
        })
        assert changed.status_code == 200, changed.text
        result = changed.json()["styling"]
        assert result["revision"] == 1 and result["status"] == "draft"
        assert result["constraints"]["target_date"] == tomorrow.isoformat()
        assert {entry["item_id"] for entry in result["outfit"]["items"]} == {str(items[i].id) for i in (0, 1, 3)}
        assert model.calls == 2
        tomorrow_daily = await SqlAlchemyRecommendationRepository(db_session).get_daily(user_id, tomorrow)
        assert tomorrow_daily is not None and str(tomorrow_daily.outfits[0].id) == base_id


async def test_memory_write_requires_current_evidence_and_forget_really_deletes(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, user_id, _, _, _ = await _setup(db_session, monkeypatch)
    repository = SqlAlchemyPreferenceMemoryRepository(db_session)
    await record_preference(repository, user_id=user_id, preference_type="color", preference_value="red",
                            polarity="negative", confidence=0.9, source="explicit_user_statement")

    class Preferences:
        text = ""

        async def polish_reason(self, prompt: str) -> str:
            return prompt

        async def ask(self, system_prompt: str, question: str, *, json_mode: bool = False) -> LlmAnswer:
            return LlmAnswer(self.text, 2)

    model = Preferences()
    app.dependency_overrides[llm_service] = lambda: model
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver",
                           headers={"X-User-Id": str(user_id)}) as client:
        model.text = json.dumps({"answer": "好", "referenced_items": [], "stated_preferences": [{
            "type": "color", "value": "blue", "polarity": "negative", "evidence_text": "旧消息中不喜欢蓝色",
        }]})
        assert (await client.post("/v1/ask", json={"question": "你好"})).status_code == 200
        assert [row.preference_value for row in await repository.list_live(user_id)] == ["red"]
        model.text = json.dumps({"answer": "好", "referenced_items": [], "stated_preferences": [{
            "operation": "forget", "type": "color", "value": "red", "polarity": "neutral",
            "scope": {}, "evidence_text": "忘记我不喜欢红色",
        }]})
        assert (await client.post("/v1/ask", json={"question": "忘记我不喜欢红色"})).status_code == 200
        assert await repository.list_live(user_id) == []
