"""A purchase remains attached to its exact design, through choices and later turns."""

import asyncio
import copy
import json
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, async_sessionmaker

from app.application.ask.answer_question import _message_for_prompt
from app.application.ask.purchase_context import load_purchase_continuation
from app.application.ports.llm import ToolAskAnswer, ToolExecutor, ToolSpec
from app.application.preferences.memory import forget_preference
from app.application.purchase.choose_purchase import choose_purchase
from app.application.purchase.decide_purchase import DecidePurchase
from app.application.style.delete_inspiration import DeleteStyleInspiration
from app.config.settings import Settings
from app.infrastructure.persistence.models.ask_conversation import AskConversation, AskConversationState, AskMessage
from app.infrastructure.persistence.models.ask_session import AskSession
from app.infrastructure.persistence.models.preference_memory import UserPreferenceMemory
from app.infrastructure.persistence.models.purchase_decision import WardrobeDecision, WardrobeDecisionChoice
from app.infrastructure.persistence.models.style import UserStyleProfile
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.preference_memory_repo import SqlAlchemyPreferenceMemoryRepository
from app.infrastructure.persistence.repositories.style_repo import SqlAlchemyStyleRepository
from app.presentation.api.v1.routers import purchase_candidates
from tests.api.test_designer_sessions import _Designer, _setup
from tests.services.test_v3_purchase_decision import _add_candidate


async def _shopping_setup(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> tuple[FastAPI, uuid.UUID, list[WardrobeItem], _Designer, Settings]:
    app, user_id, items, model, settings = await _setup(session, monkeypatch)
    settings.ENABLE_V3_PURCHASE_DECISION = True
    monkeypatch.setattr(purchase_candidates, "get_settings", lambda: settings)
    app.include_router(purchase_candidates.router)
    app.include_router(purchase_candidates.wardrobe_decisions_router)
    return app, user_id, items, model, settings


def _client(app: FastAPI, user_id: uuid.UUID) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test",
                       headers={"X-User-Id": str(user_id)})


async def _design(client: AsyncClient) -> dict[str, Any]:
    response = await client.post("/v1/ask", json={
        "question": "帮我用已有衣服搭一套", "design_mode": True, "request_id": str(uuid.uuid4()),
    })
    assert response.status_code == 200, response.text
    return response.json()


def _decision_request(turn: dict[str, Any]) -> dict[str, Any]:
    return {"conversation_id": turn["styling"]["conversation_id"],
            "expected_revision": turn["design_brief"]["revision"], "request_id": str(uuid.uuid4())}


async def test_design_purchase_choice_restore_and_natural_follow_up(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, user_id, items, model, _ = await _shopping_setup(db_session, monkeypatch)
    candidate = await _add_candidate(db_session, user_id)
    prompts: list[str] = []
    original = model.ask_with_tools

    async def capture(
        system_prompt: str, question: str, tools: Sequence[ToolSpec], execute: ToolExecutor,
        *, json_mode: bool = False, max_rounds: int = 4,
    ) -> ToolAskAnswer:
        prompts.append(system_prompt)
        return await original(system_prompt, question, tools, execute, json_mode=json_mode, max_rounds=max_rounds)

    monkeypatch.setattr(model, "ask_with_tools", capture)
    async with _client(app, user_id) as client:
        turn = await _design(client)
        request = _decision_request(turn)
        response = await client.post(f"/v1/purchase-candidates/{candidate.id}/decision", json=request)
        assert response.status_code == 200, response.text
        purchase = response.json()
        decision_id = uuid.UUID(purchase["id"])
        assert purchase["design_context"]["revision"] == 1
        assert purchase["design_context"]["source_message_id"]
        assert purchase["design_context"]["styling_revision_id"]
        stored = await db_session.get(WardrobeDecision, decision_id)
        assert stored is not None
        snapshots = copy.deepcopy((stored.context_snapshot, stored.evidence_snapshot))
        selected = await client.put(f"/v1/wardrobe-decisions/{decision_id}/choice", json={"choice": "buy"})
        assert selected.status_code == 200 and selected.json()["choice"] == "buy"
        repeated = await client.put(f"/v1/wardrobe-decisions/{decision_id}/choice", json={"choice": "buy"})
        assert repeated.json()["choice_updated_at"] == selected.json()["choice_updated_at"]
        cleared = await client.put(f"/v1/wardrobe-decisions/{decision_id}/choice", json={"choice": None})
        assert cleared.json()["choice"] is None

        follow_up = {"question": "那先不买了，继续用已有衣服搭", "conversation_id": request["conversation_id"],
                     "expected_revision": 1, "request_id": str(uuid.uuid4())}
        continued = await client.post("/v1/ask", json=follow_up)
        assert continued.status_code == 200, continued.text
        assert "Saved purchase evaluations" in prompts[-1] and "NOT wardrobe items" in prompts[-1]
        purchase_line = next(line for line in prompts[-1].splitlines() if line.startswith("Saved purchase evaluations"))
        assert '"owned": false' in purchase_line and '"choice": "skip"' in purchase_line
        assert str(candidate.id) not in continued.json()["item_refs"]
        assert set(continued.json()["item_refs"]) <= {str(item.id) for item in items}
        choice = await db_session.get(WardrobeDecisionChoice, decision_id)
        assert choice is not None and choice.choice == "skip" and choice.source == "ask"
        assert str(choice.ask_session_id) == continued.json()["ask_session_id"]
        choice_time = choice.updated_at
        replay = await client.post("/v1/ask", json=follow_up)
        assert replay.json()["ask_session_id"] == continued.json()["ask_session_id"]
        assert choice.updated_at == choice_time and model.calls == 2
        restored = await client.get(f"/v1/ask/conversations/{request['conversation_id']}")
        assert restored.json()["purchase_decisions"][0]["id"] == str(decision_id)
        assert restored.json()["purchase_decisions"][0]["choice"] == "skip"
        assert restored.json()["purchase_decisions"][0]["design_context"]["revision"] == 1
        assert restored.json()["design_brief"]["revision"] == 2
        assert (stored.context_snapshot, stored.evidence_snapshot) == snapshots
        message = await db_session.scalar(select(AskMessage).where(
            AskMessage.ask_session_id == choice.ask_session_id, AskMessage.role == "assistant",
        ))
        assert message is not None and _message_for_prompt(message, max_len=300) is None
        state = await db_session.get(AskConversationState, uuid.UUID(request["conversation_id"]))
        assert state is not None and state.last_outfit["answer"] == ""
        assert await db_session.scalar(select(func.count()).select_from(WardrobeItem)) == len(items)
        assert await db_session.scalar(select(func.count()).select_from(UserPreferenceMemory)) == 0


async def test_current_pending_brief_is_used_and_legacy_and_old_replays_stay_immutable(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, user_id, _, model, _ = await _shopping_setup(db_session, monkeypatch)
    candidate = await _add_candidate(db_session, user_id)
    async with _client(app, user_id) as client:
        turn = await _design(client)
        request = _decision_request(turn)
        path = f"/v1/purchase-candidates/{candidate.id}/decision"
        first = (await client.post(path, json=request)).json()
        legacy = (await client.post(path)).json()
        assert legacy["id"] != first["id"] and legacy["design_context"] is None
        model.clarify = True
        newer = await client.post("/v1/ask", json={
            "question": "改成正式场景，先记住条件", "conversation_id": request["conversation_id"],
            "expected_revision": 1, "request_id": str(uuid.uuid4()),
            "constraints": {"scene": "formal", "target_style": "streetwear", "target_date": "2030-06-01"},
        })
        assert newer.status_code == 200, newer.text
        assert newer.json()["design_brief"]["revision"] == 2
        assert newer.json()["design_brief"]["needs_outfit_update"] is True
        stale = await client.post(path, json={**request, "request_id": str(uuid.uuid4())})
        assert stale.status_code == 409
        assert (await client.post(path, json=request)).json() == first
        second_request = {**request, "expected_revision": 2, "request_id": str(uuid.uuid4())}
        second = await client.post(path, json=second_request)
        assert second.status_code == 200, second.text
        result = second.json()
        assert result["id"] != first["id"] and result["design_context"]["revision"] == 2
        assert result["design_context"]["constraints"]["scene"] == "formal"
        assert "target_date_weather" in result["design_context"]["unverified_dimensions"]
        assert result["decision"] == "wait"  # No formal scene evidence, unlike the unconstrained purchase.
        collision = await client.post(path, json={**second_request, "concern": "color"})
        assert collision.status_code == 409
        ask_collision = await client.post("/v1/ask", json={
            "question": "继续搭", "conversation_id": request["conversation_id"],
            "expected_revision": 2, "request_id": second_request["request_id"],
        })
        assert ask_collision.status_code == 409
        assert (await client.post(path)).json() == legacy
        assert (await client.get(f"/v1/wardrobe-decisions/{first['id']}")).json() == first


async def test_context_validation_owner_and_feature_gates(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, user_id, _, _, settings = await _shopping_setup(db_session, monkeypatch)
    candidate = await _add_candidate(db_session, user_id)
    stranger = User(id=uuid.uuid4())
    db_session.add(stranger)
    await db_session.flush()
    foreign_candidate = await _add_candidate(db_session, stranger.id)
    stranger_id, foreign_candidate_id = stranger.id, foreign_candidate.id
    async with _client(app, user_id) as client:
        turn = await _design(client)
        request = _decision_request(turn)
        path = f"/v1/purchase-candidates/{candidate.id}/decision"
        assert (await client.post(path, json={"conversation_id": request["conversation_id"]})).status_code == 422
        assert (await client.post(path, json={**request, "conversation_id": str(uuid.uuid4())})).status_code == 404
        foreign_response = await client.post(f"/v1/purchase-candidates/{foreign_candidate_id}/decision", json=request)
        assert foreign_response.status_code == 404
        response = await client.post(path, json=request)
        assert response.status_code == 200, response.text
        purchase_id = response.json()["id"]
        assert (await client.put(f"/v1/wardrobe-decisions/{purchase_id}/choice", json={})).status_code == 422
        settings.ENABLE_DESIGNER_SESSIONS = False
        assert (await client.post(path, json=request)).status_code == 503
        assert (await client.post(path)).status_code == 200
        settings.ENABLE_DESIGNER_SESSIONS = True
        settings.ENABLE_V3_PURCHASE_DECISION = False
        disabled = await client.put(f"/v1/wardrobe-decisions/{purchase_id}/choice", json={"choice": "buy"})
        assert disabled.status_code == 503
        history = await client.get(f"/v1/ask/conversations/{request['conversation_id']}")
        assert history.json()["purchase_decisions"] == []
        settings.ENABLE_V3_PURCHASE_DECISION = True
    async with _client(app, stranger_id) as client:
        assert (await client.get(f"/v1/wardrobe-decisions/{purchase_id}")).status_code == 404
        foreign = await client.put(f"/v1/wardrobe-decisions/{purchase_id}/choice", json={"choice": "buy"})
        assert foreign.status_code == 404
        own_path = f"/v1/purchase-candidates/{foreign_candidate_id}/decision"
        assert (await client.post(own_path, json=request)).status_code == 404


async def test_locked_items_are_pinned_before_the_window_and_deleted_locks_are_rejected(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, user_id, items, _, _ = await _shopping_setup(db_session, monkeypatch)
    items[0].created_at = datetime(2020, 1, 1, tzinfo=UTC)
    db_session.add_all([WardrobeItem(user_id=user_id, category="top", color_main="gray", title="recent top")
                        for _ in range(65)])
    conversation = AskConversation(user_id=user_id, styling_revision=1, design_constraints={
        "locked_item_ids": [str(items[0].id)], "excluded_item_ids": [str(items[2].id)],
    })
    db_session.add(conversation)
    candidate = await _add_candidate(db_session, user_id)
    async with _client(app, user_id) as client:
        request = {"conversation_id": str(conversation.id), "expected_revision": 1, "request_id": str(uuid.uuid4())}
        response = await client.post(f"/v1/purchase-candidates/{candidate.id}/decision", json=request)
        assert response.status_code == 200, response.text
        row = await db_session.get(WardrobeDecision, uuid.UUID(response.json()["id"]))
        assert row is not None
        decision_id, conversation_id = row.id, conversation.id
        outfits = row.evidence_snapshot["outfit_evidence"]["outfits"]
        assert outfits and all(str(items[0].id) in look["item_ids"] for look in outfits)
        assert all(str(items[2].id) not in look["item_ids"] for look in outfits)
        assert all(str(items[3].id) in look["item_ids"] for look in outfits)
        items[0].deleted_at = datetime.now(UTC)
        await db_session.commit()
        rejected = await client.post(f"/v1/purchase-candidates/{candidate.id}/decision", json={
            **request, "request_id": str(uuid.uuid4()),
        })
        assert rejected.status_code == 404
        assert (await client.get(f"/v1/wardrobe-decisions/{decision_id}")).status_code == 200
        assert not (await load_purchase_continuation(db_session, user_id, conversation_id, "继续搭")).entries


async def test_same_slot_candidate_reports_conflict_without_replacing_the_locked_piece(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, user_id, items, _, _ = await _shopping_setup(db_session, monkeypatch)
    conversation = AskConversation(user_id=user_id, styling_revision=1,
                                   design_constraints={"locked_item_ids": [str(items[0].id)]})
    db_session.add(conversation)
    candidate = await _add_candidate(db_session, user_id, parsed={"category": "top", "colors": ["white"]})
    async with _client(app, user_id) as client:
        response = await client.post(f"/v1/purchase-candidates/{candidate.id}/decision", json={
            "conversation_id": str(conversation.id), "expected_revision": 1, "request_id": str(uuid.uuid4()),
        })
        assert response.status_code == 200, response.text
        assert response.json()["decision"] == "wait"
        assert any("保留" in reason["text"] for reason in response.json()["reasons"])
        row = await db_session.get(WardrobeDecision, uuid.UUID(response.json()["id"]))
        assert row is not None
        assert row.evidence_snapshot["design_evidence"]["conflicting_locked_item_ids"] == [str(items[0].id)]
        assert row.evidence_snapshot["outfit_evidence"]["outfits"] == []
        assert row.evidence_snapshot["outfit_evidence"]["missing_categories"] == []


@pytest.mark.parametrize("revocation", ["reference", "forget"])
async def test_revoked_results_remain_readable_but_cannot_return_to_a_prompt(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, revocation: str,
) -> None:
    app, user_id, items, _, _ = await _shopping_setup(db_session, monkeypatch)
    for item in items:
        item.status = "unavailable"
    reference = UserStyleProfile(user_id=user_id, source_type="inspiration_image", image_url="https://example.com/ref")
    db_session.add(reference)
    await db_session.flush()
    conversation = AskConversation(user_id=user_id, styling_revision=1, design_constraints={
        "inspiration_id": str(reference.id),
    })
    db_session.add(conversation)
    candidate = await _add_candidate(db_session, user_id)
    row = await DecidePurchase(db_session).execute(
        user_id, candidate, conversation_id=conversation.id, expected_revision=1, request_id=uuid.uuid4(),
    )
    await db_session.commit()
    assert row.evidence_snapshot["design_evidence"]["supporting_item_ids"] == []
    before = await load_purchase_continuation(db_session, user_id, conversation.id, "再想想")
    assert before.entries and before.pending is not None
    if revocation == "reference":
        await DeleteStyleInspiration(SqlAlchemyStyleRepository(db_session)).execute(reference.id, user_id)
    else:
        await forget_preference(SqlAlchemyPreferenceMemoryRepository(db_session), user_id=user_id,
                                preference_type="color", preference_value="blue")
    await db_session.commit()
    owner = await db_session.get(User, user_id, populate_existing=True)
    assert owner is not None and owner.memory_context_reset_at is not None
    after = await load_purchase_continuation(db_session, user_id, conversation.id, "再想想",
                                            after=owner.memory_context_reset_at)
    assert not after.entries and after.pending is None
    # Also independently verify missing-reference detection without relying on the watermark.
    if revocation == "reference":
        assert not (await load_purchase_continuation(db_session, user_id, conversation.id, "继续搭")).entries
    async with _client(app, user_id) as client:
        restored = await client.get(f"/v1/wardrobe-decisions/{row.id}")
        assert restored.status_code == 200 and restored.json()["design_context"]["inspiration_id"] == str(reference.id)


@pytest.mark.parametrize("operation", ["forget", "choice"])
async def test_source_or_choice_change_during_provider_cannot_save_a_stale_follow_up(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, operation: str,
) -> None:
    app, user_id, _, model, _ = await _shopping_setup(db_session, monkeypatch)
    candidate = await _add_candidate(db_session, user_id)
    async with _client(app, user_id) as client:
        turn = await _design(client)
        request = _decision_request(turn)
        purchase = (await client.post(f"/v1/purchase-candidates/{candidate.id}/decision", json=request)).json()
        decision_id = uuid.UUID(purchase["id"])
        assert isinstance(db_session.bind, AsyncConnection)
        await db_session.bind.commit()
        sessions = async_sessionmaker(db_session.bind.engine, expire_on_commit=False)

        async def mutate() -> None:
            async with sessions.begin() as transaction:
                if operation == "forget":
                    await forget_preference(SqlAlchemyPreferenceMemoryRepository(transaction), user_id=user_id,
                                            preference_type="color", preference_value="blue")
                else:
                    await choose_purchase(transaction, user_id, decision_id, "buy")

        async def changed_during_answer(
            system_prompt: str, question: str, tools: Sequence[ToolSpec], execute: ToolExecutor,
            *, json_mode: bool = False, max_rounds: int = 4,
        ) -> ToolAskAnswer:
            assert "Saved purchase evaluations" in system_prompt
            await asyncio.wait_for(mutate(), timeout=5)
            return ToolAskAnswer(json.dumps({
                "answer": "A stale answer", "reply_kind": "general", "referenced_items": [],
            }), 5)

        monkeypatch.setattr(model, "ask_with_tools", changed_during_answer)
        response = await client.post("/v1/ask", json={
            "question": "那先不买了，继续用已有衣服搭", "conversation_id": request["conversation_id"],
            "expected_revision": 1, "request_id": str(uuid.uuid4()),
        })
        assert response.status_code == 409, response.text
        assert await db_session.scalar(select(func.count()).select_from(AskSession)) == 1
        conversation = await db_session.get(
            AskConversation, uuid.UUID(request["conversation_id"]), populate_existing=True,
        )
        assert conversation is not None and conversation.styling_revision == 1
        choice = await db_session.get(WardrobeDecisionChoice, decision_id, populate_existing=True)
        assert (choice.choice if choice else None) == ("buy" if operation == "choice" else None)


@pytest.mark.parametrize("explicit_memory", [False, True])
async def test_purchase_choice_cannot_be_promoted_to_a_durable_preference(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, explicit_memory: bool,
) -> None:
    app, user_id, _, model, _ = await _shopping_setup(db_session, monkeypatch)
    candidate = await _add_candidate(db_session, user_id)
    async with _client(app, user_id) as client:
        turn = await _design(client)
        request = _decision_request(turn)
        assert (await client.post(f"/v1/purchase-candidates/{candidate.id}/decision", json=request)).status_code == 200
        evidence = "记住我喜欢红色" if explicit_memory else "打算买"

        async def inferred_memory(
            system_prompt: str, question: str, tools: Sequence[ToolSpec], execute: ToolExecutor,
            *, json_mode: bool = False, max_rounds: int = 4,
        ) -> ToolAskAnswer:
            return ToolAskAnswer(json.dumps({
                "answer": "知道了", "reply_kind": "general", "referenced_items": [],
                "stated_preferences": [{
                    "type": "color", "value": "red", "polarity": "positive", "evidence_text": evidence,
                }],
            }, ensure_ascii=False), 5)

        monkeypatch.setattr(model, "ask_with_tools", inferred_memory)
        response = await client.post("/v1/ask", json={
            "question": "我打算买这件" + ("，记住我喜欢红色" if explicit_memory else ""),
            "conversation_id": request["conversation_id"], "expected_revision": 1, "request_id": str(uuid.uuid4()),
        })
        assert response.status_code == 200, response.text
        assert await db_session.scalar(select(func.count()).select_from(UserPreferenceMemory)) == int(explicit_memory)


async def test_ambiguous_choice_with_two_nearby_candidates_is_not_assigned_to_the_latest(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, user_id, _, _, _ = await _shopping_setup(db_session, monkeypatch)
    first = await _add_candidate(db_session, user_id)
    second = await _add_candidate(db_session, user_id)
    async with _client(app, user_id) as client:
        turn = await _design(client)
        for candidate in (first, second):
            request = _decision_request(turn)
            response = await client.post(f"/v1/purchase-candidates/{candidate.id}/decision", json=request)
            assert response.status_code == 200, response.text
        continuation = await load_purchase_continuation(
            db_session, user_id, uuid.UUID(turn["styling"]["conversation_id"]), "那先不买了，继续用已有衣服搭",
        )
        assert len(continuation.entries) == 2 and continuation.pending is None
