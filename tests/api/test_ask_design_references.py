"""Reference images stay owned, genuinely analysed, revocable and task-scoped."""

import uuid
from collections.abc import Sequence
from typing import Any

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from app.application.ports.llm import ToolAskAnswer, ToolExecutor, ToolSpec
from app.bootstrap.container import llm_service
from app.infrastructure.persistence.models.preference_memory import UserPreferenceMemory
from app.infrastructure.persistence.models.style import UserStyleProfile
from app.infrastructure.persistence.models.user import User
from app.presentation.api.v1.routers import style
from tests.api.test_designer_sessions import _Designer, _setup


class _ReferenceDesigner(_Designer):
    def __init__(self) -> None:
        super().__init__()
        self.prompts: list[str] = []
        self.rankings: list[list[dict[str, Any]]] = []

    async def ask_with_tools(
        self, system_prompt: str, question: str, tools: Sequence[ToolSpec], execute: ToolExecutor,
        *, json_mode: bool = False, max_rounds: int = 4,
    ) -> ToolAskAnswer:
        self.prompts.append(system_prompt)
        answer = await super().ask_with_tools(
            system_prompt, question, tools, execute, json_mode=json_mode, max_rounds=max_rounds,
        )
        result = await execute("search_wardrobe", {"category": "top"})
        self.rankings.append(result.content["items"])
        return answer


async def _reference(
    session: AsyncSession, user_id: uuid.UUID, *, source: str = "vision", status: str = "learned",
) -> UserStyleProfile:
    row = UserStyleProfile(
        user_id=user_id, source_type="inspiration_image", image_url="https://cdn.example.com/reference.jpg",
        learn_status=status, weight=1,
        learned_summary={"source": source, "style_tags": ["vintage"], "color_palette": ["red"],
                         "confidence": 0.8},
    )
    session.add(row)
    await session.flush()
    await session.commit()
    assert isinstance(session.bind, AsyncConnection)
    await session.bind.commit()
    return row


async def test_reference_reaches_candidate_ranking_and_survives_resume_without_writing_preference(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, user_id, _, _, settings = await _setup(db_session, monkeypatch)
    settings.ENABLE_VISION_STYLE_LEARNING = True
    monkeypatch.setattr(style, "get_settings", lambda: settings)
    app.include_router(style.router)
    model = _ReferenceDesigner()
    app.dependency_overrides[llm_service] = lambda: model
    reference = await _reference(db_session, user_id)
    request = {"question": "参考这张图，帮我用衣橱搭一套", "design_mode": True,
               "request_id": str(uuid.uuid4()), "constraints": {"reference_inspiration_id": str(reference.id)}}

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver",
                           headers={"X-User-Id": str(user_id)}) as client:
        listed = (await client.get("/v1/style/inspirations")).json()["data"]["items"]
        assert listed[0]["design_reference_available"] is True
        response = await client.post("/v1/ask", json=request)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["design_brief"]["constraints"]["reference_inspiration_id"] == str(reference.id)
        assert "Selected inspiration's image analysis" in model.prompts[0]
        assert '"color_palette": ["red"]' in model.prompts[0]
        assert model.rankings[0][0]["color_main"] == "red"
        assert str(reference.id) not in model.prompts[0]
        assert reference.image_url not in model.prompts[0]
        assert await db_session.scalar(select(func.count()).select_from(UserPreferenceMemory)) == 0

        conversation_id = body["conversation_id"]
        restored = (await client.get(f"/v1/ask/conversations/{conversation_id}")).json()
        assert restored["design_brief"]["constraints"]["reference_inspiration_id"] == str(reference.id)
        replay = await client.post("/v1/ask", json=request)
        assert replay.json()["ask_session_id"] == body["ask_session_id"] and model.calls == 1

        # An inherited reference becoming unavailable must not strand the conversation.
        settings.ENABLE_VISION_STYLE_LEARNING = False
        listed = (await client.get("/v1/style/inspirations")).json()["data"]["items"]
        assert listed[0]["design_reference_available"] is False
        continued = await client.post("/v1/ask", json={
            "question": "保留这套，再解释一下", "conversation_id": conversation_id,
            "request_id": str(uuid.uuid4()), "expected_revision": body["design_brief"]["revision"],
        })
        assert continued.status_code == 200, continued.text
        assert "Selected inspiration's image analysis" not in model.prompts[-1]
        assert "previously selected image analysis is unavailable" in model.prompts[-1]


@pytest.mark.parametrize("case,status", [("foreign", 404), ("heuristic", 409), ("queued", 409), ("off", 503)])
async def test_reference_rejected_before_model_spend(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, case: str, status: int,
) -> None:
    app, user_id, _, model, settings = await _setup(db_session, monkeypatch)
    settings.ENABLE_VISION_STYLE_LEARNING = case != "off"
    owner = user_id
    if case == "foreign":
        owner = uuid.uuid4()
        db_session.add(User(id=owner))
        await db_session.flush()
    reference = await _reference(
        db_session, owner, source="heuristic" if case == "heuristic" else "vision",
        status="queued" if case == "queued" else "learned",
    )
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver",
                           headers={"X-User-Id": str(user_id)}) as client:
        response = await client.post("/v1/ask", json={
            "question": "照这个感觉搭", "design_mode": True, "request_id": str(uuid.uuid4()),
            "constraints": {"reference_inspiration_id": str(reference.id)},
        })
    assert response.status_code == status, response.text
    assert model.calls == 0
