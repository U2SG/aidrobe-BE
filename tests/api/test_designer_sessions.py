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
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.application.packing import generate as packing_generate
from app.application.ports.llm import LlmAnswer, ToolAskAnswer, ToolExecutor, ToolSpec
from app.application.preferences.memory import read_preference_memory, record_preference
from app.bootstrap.container import llm_service
from app.common.item_labels import localized_item_name
from app.config.settings import Settings, get_settings
from app.infrastructure.cache.client import get_redis_client
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.outfit_visual import OutfitVisualAsset
from app.infrastructure.persistence.models.user import User, UserSettings
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.preference_memory_repo import SqlAlchemyPreferenceMemoryRepository
from app.infrastructure.persistence.repositories.recommendation_repo import SqlAlchemyRecommendationRepository
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import ask, outfit_visual, packing_list, recommendations
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
        self.clarify = False
        self.lock_top = False
        self.seen_briefs: list[dict[str, Any]] = []
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
        brief_line = next(line for line in system_prompt.splitlines() if line.startswith("Design session: "))
        self.seen_briefs.append(json.loads(brief_line.removeprefix("Design session: ")))

        def position(category: str, color: str) -> int:
            return next(index for index, item in enumerate(known, 1)
                        if item["category"] == category and item["color_main"] == color)

        brief: dict[str, Any] = {"evidence_text": question, "goal": question}
        if self.lock_top:
            brief["locked_positions"] = [position("top", "white")]
        if self.change_shoes:
            brief["change_positions"] = [position("shoes", "black")]
        if self.explore:
            brief.update(
                exploration=True, target_style="streetwear", preference_overrides=[{"type": "color", "value": "red"}]
            )
        result = await execute("update_design_brief", brief)
        assert result.content["updated"] is True
        self.preferences_after_update = result.content["preferences"]
        if self.clarify:
            return ToolAskAnswer(json.dumps({
                "answer": "条件已经记下，想要更正式还是更轻松一点？", "reply_kind": "general", "referenced_items": [],
            }, ensure_ascii=False), 7)
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
    # Tools read on independent connections so cancelling them cannot invalidate
    # the request transaction. Seed committed rows, as production requests see.
    await session.commit()
    assert isinstance(session.bind, AsyncConnection)
    await session.bind.commit()
    model = _Designer()
    app = FastAPI()
    setup_exception_handlers(app)
    async def request_session() -> AsyncIterator[AsyncSession]:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise

    app.dependency_overrides[get_db_session] = request_session
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


async def test_saved_packing_list_returns_to_the_original_design(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, user_id, items, model, settings = await _setup(db_session, monkeypatch)
    settings.ENABLE_PACKING_LIST = True
    settings.ENABLE_PACKING_PLANNER = True
    for module in (packing_list, packing_generate):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    app.include_router(packing_list.router)
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://testserver",
        headers={"X-User-Id": str(user_id)},
    ) as client:
        design = await client.post("/v1/ask", json={
            "question": "设计一套旅行搭配", "design_mode": True, "request_id": str(uuid.uuid4()),
        })
        assert design.status_code == 200, design.text
        turn = design.json()
        conversation_id = turn["conversation_id"]
        saved_outfit = turn["styling"]["outfit"]["outfit_id"]
        created = await client.post("/v1/packing-lists", json={
            "conversation_id": conversation_id, "expected_revision": 1,
            "destination": "苏州", "days": 2, "scenes": ["casual"],
        })
        assert created.status_code == 200, created.text
        packing = created.json()
        assert packing["conversation_id"] == conversation_id
        assert set(packing["plan"]["daily_outfits"][0]["item_ids"]) == {str(item.id) for item in items[:3]}
        outfit_path = f"/v1/ask/conversations/{conversation_id}"
        restored = await client.get(outfit_path)
        assert restored.json()["packing_list"] == packing
        calls = model.calls
        question = {
            "question": "帮我读一下刚保存的苏州打包清单", "design_mode": True,
            "request_id": str(uuid.uuid4()), "conversation_id": conversation_id, "expected_revision": 1,
        }
        reply = await client.post("/v1/ask", json=question)
        assert reply.status_code == 200, reply.text
        answer = reply.json()
        assert answer["conversation_id"] == conversation_id
        assert "苏州" in answer["answer"] and "第 2 天" in answer["answer"]
        top = items[0]
        assert localized_item_name(
            title=top.title, color_main=top.color_main, category=top.category, locale="zh-CN",
            localized_names=top.localized_names, name_source=top.name_source,
        ) in answer["answer"]
        assert model.calls == calls and answer["tokens_used"] == 0
        reopened = await client.get(outfit_path)
        assert reopened.json()["messages"][-1]["content"] == answer["answer"]
        assert reopened.json()["styling"]["outfit"]["outfit_id"] == saved_outfit

        model.change_shoes = True
        edited = await client.post("/v1/ask", json={
            **question, "request_id": str(uuid.uuid4()),
            "question": "看一下打包清单，把这套的鞋换成棕色那双",
        })
        assert edited.status_code == 200, edited.text
        assert edited.json()["styling"]["revision"] == 2 and model.calls == calls + 1
        assert (await client.get(outfit_path)).json()["packing_list"] == packing


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
        assert rejected.json()["design_brief"]["revision"] == 2
        assert rejected.json()["design_brief"]["needs_outfit_update"] is True
        assert rejected.json()["item_refs"] == []
        model.change_unrequested_top = False
        accepted = await client.post("/v1/ask", json={
            "question": "只换鞋，其他保持", "design_mode": True, "request_id": str(uuid.uuid4()),
            "conversation_id": first["conversation_id"], "expected_revision": 2,
        })
        assert accepted.status_code == 200, accepted.text
        latest = accepted.json()["styling"]
        assert latest["revision"] == 3
        assert {entry["item_id"] for entry in latest["outfit"]["items"]} == {str(items[i].id) for i in (0, 1, 3)}


async def test_exploration_exception_applies_this_turn_without_rewriting_memory(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, user_id, items, model, _ = await _setup(db_session, monkeypatch)
    repository = SqlAlchemyPreferenceMemoryRepository(db_session)
    await record_preference(repository, user_id=user_id, preference_type="color", preference_value="red",
                            polarity="negative", confidence=0.9, source="explicit_user_statement")
    await db_session.commit()
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


@pytest.mark.parametrize("has_outfit", [False, True])
async def test_clarification_preserves_conditions_history_retry_and_revision(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, has_outfit: bool,
) -> None:
    app, user_id, items, model, _ = await _setup(db_session, monkeypatch)
    top_id = str(items[0].id)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver",
                           headers={"X-User-Id": str(user_id)}) as client:
        first = None
        if has_outfit:
            first = (await client.post("/v1/ask", json={
                "question": "先搭一套", "design_mode": True, "request_id": str(uuid.uuid4()),
            })).json()
        model.clarify = True
        model.lock_top = True
        request = {
            "question": "保留白色上衣，先聊聊场景", "design_mode": True, "request_id": str(uuid.uuid4()),
            **({"conversation_id": first["conversation_id"], "expected_revision": 1} if first else {}),
        }
        response = await client.post("/v1/ask", json=request)
        assert response.status_code == 200, response.text
        clarified = response.json()
        brief = clarified["design_brief"]
        expected = 2 if has_outfit else 1
        assert brief["revision"] == expected and brief["needs_outfit_update"] is True
        assert brief["constraints"]["locked_item_ids"] == [top_id]
        assert clarified["styling"] == (first["styling"] if first else None)
        conversation_id = clarified["conversation_id"]
        restored = (await client.get(f"/v1/ask/conversations/{conversation_id}")).json()
        assert restored["design_brief"] == brief
        assert restored["messages"][-1]["design_brief"] == brief
        assert restored["styling"] == clarified["styling"]
        if has_outfit:
            adoption = await client.post(f"/v1/ask/conversations/{conversation_id}/adopt", json={
                "request_id": str(uuid.uuid4()), "expected_revision": expected,
            })
            assert adoption.status_code == 409
            edit = await client.post(f"/v1/ask/conversations/{conversation_id}/outfit-revisions", json={
                "request_id": str(uuid.uuid4()), "expected_revision": expected, "locked_item_ids": [],
            })
            assert edit.status_code == 409
        model.clarify = False
        model.lock_top = False
        completed = await client.post("/v1/ask", json={
            "question": "轻松一点，按刚才的条件搭", "request_id": str(uuid.uuid4()),
            "conversation_id": conversation_id, "expected_revision": expected,
        })
        assert completed.status_code == 200, completed.text
        result = completed.json()
        assert result["styling"]["revision"] == expected + 1
        assert result["styling"]["constraints"]["locked_item_ids"] == [top_id]
        assert result["design_brief"]["needs_outfit_update"] is False
        assert len(model.seen_briefs[-1]["locked_positions"]) == 1
        retry = await client.post("/v1/ask", json=request)
        assert retry.status_code == 200 and retry.json()["design_brief"] == brief
        assert retry.json()["ask_session_id"] == clarified["ask_session_id"]
        latest = (await client.get(f"/v1/ask/conversations/{conversation_id}")).json()
        assert latest["design_brief"]["revision"] == expected + 1
        assert latest["messages"][-3]["design_brief"] == brief


async def test_first_clarification_keeps_the_today_base_for_follow_up(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, user_id, items, model, _ = await _setup(db_session, monkeypatch)
    expected_items = {str(items[index].id) for index in (0, 1, 3)}
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver",
                           headers={"X-User-Id": str(user_id)}) as client:
        first = (await client.post("/v1/ask", json={
            "question": "今天穿这套", "design_mode": True, "request_id": str(uuid.uuid4()),
        })).json()
        base_id = first["styling"]["outfit"]["outfit_id"]
        model.clarify = True
        pending = (await client.post("/v1/ask", json={
            "question": "这套我有点想调整，先聊聊", "design_mode": True,
            "request_id": str(uuid.uuid4()), "base_outfit_id": base_id,
        })).json()
        assert pending["styling"] is None and pending["design_brief"]["revision"] == 1
        model.clarify = False
        model.change_shoes = True
        completed = await client.post("/v1/ask", json={
            "question": "只换鞋，其他保持", "request_id": str(uuid.uuid4()),
            "conversation_id": pending["conversation_id"], "expected_revision": 1,
        })
        assert completed.status_code == 200, completed.text
        assert len(model.seen_briefs[-1]["current_positions"]) == 3
        assert {item["item_id"] for item in completed.json()["styling"]["outfit"]["items"]} == expected_items


async def test_plain_conversation_restore_keeps_item_cards_and_trip_handoff(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, user_id, items, _, settings = await _setup(db_session, monkeypatch, enabled=False)
    settings.ENABLE_ASK_CONVERSATIONS = True
    item_id = str(items[0].id)

    class Plain:
        async def ask(self, system_prompt: str, question: str, *, json_mode: bool = False) -> LlmAnswer:
            return LlmAnswer(json.dumps({
                "answer": "带上这件白色上衣。", "referenced_items": [1],
                "trip_intent": {"is_trip": True, "duration_days": 3},
            }), 4)

    app.dependency_overrides[llm_service] = lambda: Plain()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver",
                           headers={"X-User-Id": str(user_id)}) as client:
        reply = (await client.post("/v1/ask", json={
            "question": "出门三天，这件怎么带？", "focused_item_id": item_id,
        })).json()
        restored = (await client.get(f"/v1/ask/conversations/{reply['conversation_id']}")).json()
        message = restored["messages"][-1]
        assert message["ask_session_id"] == reply["ask_session_id"]
        assert message["item_refs"] == [item_id]
        assert [item["item_id"] for item in message["item_ref_snapshots"]] == [item_id]
        assert message["trip_intent"] == {"is_trip": True, "duration_days": 3}
        assert message["styling"] is None and restored["design_brief"] is None
        assert restored["messages"][0]["item_refs"] == []
