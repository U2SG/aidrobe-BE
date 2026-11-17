import uuid
from unittest.mock import AsyncMock

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.preferences.memory import record_preference
from app.infrastructure.persistence.models.style import UserStyleProfile
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.repositories.preference_memory_repo import SqlAlchemyPreferenceMemoryRepository
from app.presentation.api.v1.routers import ask
from tests.api.test_designer_sessions import _setup


@pytest.mark.parametrize(("memory_enabled", "overridden", "locked", "expected_top"), [
    (True, False, False, 0),
    (True, True, False, 4),
    (False, False, False, 4),
    (True, False, True, 4),
])
async def test_quick_inspiration_ranks_effective_preferences_before_photo_matches(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
    memory_enabled: bool, overridden: bool, locked: bool, expected_top: int,
) -> None:
    app, user_id, items, model, settings = await _setup(db_session, monkeypatch)
    settings.ENABLE_PREFERENCE_MEMORY = memory_enabled
    repository = SqlAlchemyPreferenceMemoryRepository(db_session)
    await record_preference(
        repository, user_id=user_id, preference_type="color", preference_value="red",
        polarity="negative", confidence=0.9, source="explicit_user_statement",
    )
    source = UserStyleProfile(
        user_id=user_id, source_type="inspiration_image", learn_status="learned",
        learned_summary={"style_tags": [], "color_palette": ["red", "black"], "source": "vision"},
    )
    db_session.add(source)
    await db_session.commit()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver",
                           headers={"X-User-Id": str(user_id)}) as client:
        response = await client.post("/v1/ask", json={
            "question": "参考这张图，从我的衣橱里搭一套。",
            "inspiration_id": str(source.id), "inspiration_only": True,
            "request_id": str(uuid.uuid4()),
            "constraints": {
                "preference_overrides": [{"type": "color", "value": "red"}] if overridden else [],
                "locked_item_ids": [str(items[4].id)] if locked else [],
            },
        })
    assert response.status_code == 200, response.text
    selected = {row["item_id"] for row in response.json()["styling"]["outfit"]["items"]}
    assert str(items[expected_top].id) in selected
    assert str(items[4 if expected_top == 0 else 0].id) not in selected
    assert model.calls == 0
    assert [(row.preference_value, row.polarity) for row in await repository.list_live(user_id)] == [
        ("red", "negative"),
    ]


async def test_queued_inspiration_uses_visual_palette_to_save_and_restore_a_draft(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, user_id, items, model, _ = await _setup(db_session, monkeypatch)
    items[0].style_tags = {"tags": ["casual"]}
    items[4].style_tags = ["minimal"]
    source = UserStyleProfile(
        user_id=user_id, source_type="inspiration_image", learn_status="queued",
        image_url="https://cdn.example.com/reference.jpg",
    )
    db_session.add(source)
    await db_session.commit()
    analysis = AsyncMock(return_value={
        "tags": ["minimal", "red", "black"], "style_tags": ["minimal"],
        "color_palette": ["red", "black"], "source": "vision",
    })
    monkeypatch.setattr(ask.ask_uc, "learn_from_image", analysis)
    quota = AsyncMock(side_effect=AssertionError("An inspiration draft does not spend chat tokens"))
    monkeypatch.setattr(ask.ask_uc, "assert_within_quota", quota)
    request = {
        "question": "用这张灵感图搭一套苏州两天旅行穿搭",
        "inspiration_id": str(source.id),
        "inspiration_only": True,
        "request_id": str(uuid.uuid4()),
        "constraints": {
            "locked_item_ids": [str(items[1].id)],
            "inspiration_id": str(uuid.uuid4()),
        },
    }
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver",
                           headers={"X-User-Id": str(user_id)}) as client:
        response = await client.post("/v1/ask", json=request)
        assert response.status_code == 200, response.text
        draft = response.json()
        outfit = draft["styling"]["outfit"]
        assert draft["styling"]["status"] == "draft"
        assert {row["item_id"] for row in outfit["items"]} == {str(items[i].id) for i in (4, 1, 2)}
        assert draft["design_brief"]["constraints"]["inspiration_id"] == str(source.id)
        assert draft["design_brief"]["constraints"]["scene"] == "travel"
        assert draft["design_brief"]["constraints"]["target_style"] == "minimal"

        replay = await client.post("/v1/ask", json=request)
        assert replay.json()["styling"]["outfit"]["outfit_id"] == outfit["outfit_id"]
        restored = await client.get(f"/v1/ask/conversations/{draft['conversation_id']}")
        assert restored.json()["styling"] == draft["styling"]
        assert restored.json()["design_brief"] == draft["design_brief"]

    analysis.assert_awaited_once()
    quota.assert_not_awaited()
    assert model.calls == 0
    await db_session.refresh(source)
    assert source.learn_status == "learned"
    assert source.learned_summary == analysis.return_value


async def test_custom_inspiration_question_supplies_visual_context_and_waits_for_verification(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, user_id, _, model, _ = await _setup(db_session, monkeypatch)
    source = UserStyleProfile(
        user_id=user_id, source_type="inspiration_image", learn_status="learned",
        image_url="https://cdn.example.com/private-reference.jpg",
        learned_summary={"style_tags": ["minimal"], "color_palette": ["red", "black"], "source": "vision"},
    )
    db_session.add(source)
    await db_session.commit()
    # A clarification must never turn into an automatically generated outfit.
    model.clarify = True
    provider = AsyncMock(wraps=model.ask_with_tools)
    monkeypatch.setattr(model, "ask_with_tools", provider)
    question = "参考这张图搭配，但不要红色"
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver",
                           headers={"X-User-Id": str(user_id)}) as client:
        response = await client.post("/v1/ask", json={
            "question": question,
            "inspiration_id": str(source.id),
            "request_id": str(uuid.uuid4()),
        })
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["styling"] is None
    assert body["design_brief"]["constraints"]["inspiration_id"] == str(source.id)
    assert body["design_brief"]["constraints"]["goal"] == question
    provider.assert_awaited_once()
    prompt, user_text, *_ = provider.call_args.args
    assert user_text == question
    assert 'Reference: {"styles": ["minimal"], "palette": ["black", "red"]}' in prompt
    assert str(source.id) not in prompt
    assert "private-reference.jpg" not in prompt


async def test_foreign_inspiration_cannot_start_a_design(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, user_id, _, model, _ = await _setup(db_session, monkeypatch)
    other_user = User()
    db_session.add(other_user)
    await db_session.flush()
    source = UserStyleProfile(user_id=other_user.id, source_type="inspiration_image", learn_status="queued")
    db_session.add(source)
    await db_session.commit()
    analysis = AsyncMock()
    monkeypatch.setattr(ask.ask_uc, "learn_from_image", analysis)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver",
                           headers={"X-User-Id": str(user_id)}) as client:
        response = await client.post("/v1/ask", json={
            "question": "用这张灵感图设计",
            "inspiration_id": str(source.id),
            "request_id": str(uuid.uuid4()),
        })
    assert response.status_code == 404
    analysis.assert_not_awaited()
    assert model.calls == 0
