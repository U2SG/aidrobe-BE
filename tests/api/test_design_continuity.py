"""Existing shopping/travel actions retain an owned, immutable source design."""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ask import continuity
from app.application.ask.answer_question import _linked_design_context, _summarize_old_messages
from app.application.packing import generate as packing_generate
from app.config.settings import Settings
from app.infrastructure.persistence.models.ask_conversation import AskConversation, AskMessage
from app.infrastructure.persistence.models.packing_list import PackingList
from app.infrastructure.persistence.models.purchase_decision import PurchaseCandidate, WardrobeDecision
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.presentation.api.v1.routers import packing_list, purchase_candidates
from tests.api.test_designer_sessions import _Designer, _setup


async def _app(
    session: AsyncSession, monkeypatch: pytest.MonkeyPatch, *, planner: bool = False,
) -> tuple[FastAPI, uuid.UUID, list[WardrobeItem], _Designer, Settings]:
    app, user_id, items, model, settings = await _setup(session, monkeypatch)
    settings.ENABLE_V3_PURCHASE_DECISION = True
    settings.ENABLE_PACKING_LIST = True
    settings.ENABLE_PACKING_PLANNER = planner
    for module in (continuity, packing_generate, packing_list, purchase_candidates):
        monkeypatch.setattr(module, "get_settings", lambda: settings)
    monkeypatch.setattr(packing_list, "weather_service", lambda: None)
    app.include_router(purchase_candidates.router)
    app.include_router(purchase_candidates.wardrobe_decisions_router)
    app.include_router(packing_list.router)
    return app, user_id, items, model, settings


def _client(app: FastAPI, user_id: uuid.UUID) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver",
                       headers={"X-User-Id": str(user_id)})


async def _design(client: AsyncClient, items: list[WardrobeItem]) -> dict[str, Any]:
    response = await client.post("/v1/ask", json={
        "question": "保留舒服的黑鞋，继续设计", "design_mode": True, "request_id": str(uuid.uuid4()),
        "constraints": {
            "locked_item_ids": [str(items[2].id)], "excluded_item_ids": [str(items[4].id)],
            "target_style": "casual", "scene": "commute", "target_date": date.today().isoformat(),
        },
    })
    assert response.status_code == 200, response.text
    return response.json()


def _context(turn: dict[str, Any]) -> dict[str, Any]:
    return {"conversation_id": turn["conversation_id"], "expected_revision": turn["design_brief"]["revision"]}


async def _candidate(session: AsyncSession, user_id: uuid.UUID, category: str = "outerwear") -> uuid.UUID:
    candidate = PurchaseCandidate(
        user_id=user_id, source_type="share_link", source_url="https://shop.example.com/coat",
        status="parsed", parsed_item={"category": category, "colors": ["gray"], "scene_tags": ["casual"]},
    )
    session.add(candidate)
    await session.flush()
    candidate_id = candidate.id
    await session.commit()
    return candidate_id


async def test_purchase_uses_design_constraints_and_returns_to_the_same_conversation(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, user_id, items, _, _ = await _app(db_session, monkeypatch)
    async with _client(app, user_id) as client:
        turn = await _design(client, items)
        candidate_id = await _candidate(db_session, user_id)
        payload = {"design_context": _context(turn)}
        response = await client.post(f"/v1/purchase-candidates/{candidate_id}/decision", json=payload)
        assert response.status_code == 200, response.text
        decision = response.json()
        context = decision["design_context"]
        assert context["conversation_id"] == turn["conversation_id"]
        assert context["outfit_id"] == turn["styling"]["outfit"]["outfit_id"]
        assert context["constraints"]["locked_item_ids"] == [str(items[2].id)]
        stored = await db_session.get(WardrobeDecision, uuid.UUID(decision["id"]))
        assert stored is not None
        evidence = stored.evidence_snapshot["outfit_evidence"]
        assert evidence["outfits"]
        for outfit in evidence["outfits"]:
            assert str(items[2].id) in outfit["item_ids"]
            assert str(items[4].id) not in outfit["item_ids"]
        restored = (await client.get(f"/v1/ask/conversations/{turn['conversation_id']}")).json()
        linked = [message["linked_result"] for message in restored["messages"] if message.get("linked_result")]
        assert linked == [{
            "kind": "purchase_decision", "resource_id": decision["id"],
            "source_revision": context["revision"], "summary": linked[0]["summary"],
        }]
        assert decision["decision_text"] in linked[0]["summary"]
        assert (await client.get(f"/v1/wardrobe-decisions/{decision['id']}")).json() == decision
        assert (await client.post(f"/v1/purchase-candidates/{candidate_id}/decision", json=payload)).json() == decision
        assert await db_session.scalar(select(func.count()).select_from(AskMessage).where(
            AskMessage.conversation_id == uuid.UUID(turn["conversation_id"]),
            AskMessage.message_metadata["linked_result"].is_not(None),
        )) == 1


@pytest.mark.parametrize("planner", [False, True])
async def test_travel_keeps_locked_pieces_and_exclusions_with_explicit_trip_dates(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, planner: bool,
) -> None:
    app, user_id, items, _, _ = await _app(db_session, monkeypatch, planner=planner)
    async with _client(app, user_id) as client:
        turn = await _design(client, items)
        departure = date.today() + timedelta(days=12)
        payload = {"destination": "Shanghai", "days": 1, "scenes": ["casual"],
                   "departure_date": departure.isoformat(), "design_context": _context(turn),
                   "request_id": str(uuid.uuid4())}
        response = await client.post("/v1/packing-lists", json=payload)
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["departure_date"] == departure.isoformat()
        assert result["scenes"] == ["casual"]
        selected = {entry["item_id"] for entry in result["items"]}
        assert str(items[2].id) in selected and str(items[4].id) not in selected
        assert result["design_context"]["constraints"]["scene"] == "commute"
        assert result["design_context"]["outfit_id"] == turn["styling"]["outfit"]["outfit_id"]
        assert (await client.get(f"/v1/packing-lists/{result['packing_list_id']}")).json() == result
        assert (await client.post("/v1/packing-lists", json=payload)).json() == result
        assert await db_session.scalar(select(func.count()).select_from(PackingList)) == 1
        restored = (await client.get(f"/v1/ask/conversations/{turn['conversation_id']}")).json()
        link = next(message["linked_result"] for message in restored["messages"] if message.get("linked_result"))
        assert link["kind"] == "packing_list" and link["resource_id"] == result["packing_list_id"]


async def test_pending_conditions_carry_forward_without_claiming_the_old_outfit(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, user_id, items, model, _ = await _app(db_session, monkeypatch)
    async with _client(app, user_id) as client:
        turn = await _design(client, items)
        model.clarify = True
        pending_response = await client.post("/v1/ask", json={
            "question": "我现在想再轻松一点", "design_mode": True, "request_id": str(uuid.uuid4()),
            **_context(turn),
        })
        assert pending_response.status_code == 200, pending_response.text
        pending = pending_response.json()
        assert pending["design_brief"]["needs_outfit_update"] is True
        response = await client.post("/v1/packing-lists", json={
            "days": 1, "scenes": ["casual"], "design_context": _context(pending),
            "request_id": str(uuid.uuid4()),
        })
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["design_context"]["outfit_id"] is None
        assert result["design_context"]["constraints"]["goal"] == "我现在想再轻松一点"
        assert str(items[2].id) in {entry["item_id"] for entry in result["items"]}


async def test_retries_replay_the_source_version_and_new_stale_actions_conflict(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, user_id, items, model, _ = await _app(db_session, monkeypatch)
    async with _client(app, user_id) as client:
        turn = await _design(client, items)
        candidate_id = await _candidate(db_session, user_id)
        decision_payload = {"design_context": _context(turn)}
        decision = await client.post(f"/v1/purchase-candidates/{candidate_id}/decision", json=decision_payload)
        assert decision.status_code == 200, decision.text
        travel_payload = {"days": 1, "scenes": ["casual"], "design_context": _context(turn),
                          "request_id": str(uuid.uuid4())}
        travel = await client.post("/v1/packing-lists", json=travel_payload)
        assert travel.status_code == 200, travel.text
        model.clarify = True
        updated = await client.post("/v1/ask", json={
            "question": "先不要生成，我想再想一想", "design_mode": True,
            "request_id": str(uuid.uuid4()), **_context(turn),
        })
        assert updated.status_code == 200, updated.text
        assert (await client.post(f"/v1/purchase-candidates/{candidate_id}/decision",
                                  json=decision_payload)).json() == decision.json()
        assert (await client.post("/v1/packing-lists", json=travel_payload)).json() == travel.json()
        stale = await client.post("/v1/packing-lists", json={**travel_payload, "request_id": str(uuid.uuid4())})
        assert stale.status_code == 409
        reused = await client.post("/v1/packing-lists", json={**travel_payload, "days": 3})
        assert reused.status_code == 409
        rebound = await client.post(f"/v1/purchase-candidates/{candidate_id}/decision",
                                    json={"design_context": _context(updated.json())})
        assert rebound.status_code == 409


async def test_foreign_context_missing_request_and_locked_purchase_replacement_are_rejected(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, user_id, items, _, _ = await _app(db_session, monkeypatch)
    async with _client(app, user_id) as client:
        turn = await _design(client, items)
        missing_request = await client.post("/v1/packing-lists", json={
            "days": 1, "scenes": ["casual"], "design_context": _context(turn),
        })
        assert missing_request.status_code == 422
        foreign = User(id=uuid.uuid4())
        db_session.add(foreign)
        await db_session.flush()
        conversation = AskConversation(user_id=foreign.id)
        db_session.add(conversation)
        await db_session.flush()
        foreign_id = str(conversation.id)
        await db_session.commit()
        rejected = await client.post("/v1/packing-lists", json={
            "days": 1, "scenes": ["casual"], "request_id": str(uuid.uuid4()),
            "design_context": {"conversation_id": foreign_id, "expected_revision": 0},
        })
        assert rejected.status_code == 404
        candidate_id = await _candidate(db_session, user_id, "shoes")
        replacement = await client.post(f"/v1/purchase-candidates/{candidate_id}/decision",
                                        json={"design_context": _context(turn)})
        assert replacement.status_code == 409
        assert await db_session.scalar(select(func.count()).select_from(WardrobeDecision)) == 0


async def test_linked_results_are_revocable_model_context_and_never_durable_summary(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, user_id, items, _, settings = await _app(db_session, monkeypatch)
    async with _client(app, user_id) as client:
        turn = await _design(client, items)
        conversation_id = uuid.UUID(turn["conversation_id"])
        candidate_id = await _candidate(db_session, user_id)
        response = await client.post(f"/v1/purchase-candidates/{candidate_id}/decision",
                                     json={"design_context": _context(turn)})
        assert response.status_code == 200, response.text
        result = response.json()
        prompt_context = await _linked_design_context(db_session, user_id, conversation_id)
        assert prompt_context is not None and result["decision_text"] in prompt_context
        message = await db_session.scalar(select(AskMessage).where(
            AskMessage.conversation_id == conversation_id,
            AskMessage.message_metadata["linked_result"]["resource_id"].astext == result["id"],
        ))
        assert message is not None
        assert _summarize_old_messages(None, [message]) is None
        settings.ENABLE_V3_PURCHASE_DECISION = False
        assert await _linked_design_context(db_session, user_id, conversation_id) is None
        hidden = (await client.get(f"/v1/ask/conversations/{conversation_id}")).json()
        assert not any(message.get("linked_result") for message in hidden["messages"])
        settings.ENABLE_V3_PURCHASE_DECISION = True
        assert await _linked_design_context(db_session, user_id, conversation_id) is not None
        await db_session.execute(delete(WardrobeDecision).where(WardrobeDecision.id == uuid.UUID(result["id"])))
        await db_session.commit()
        assert await _linked_design_context(db_session, user_id, conversation_id) is None
        hidden = (await client.get(f"/v1/ask/conversations/{conversation_id}")).json()
        assert not any(message.get("linked_result") for message in hidden["messages"])
