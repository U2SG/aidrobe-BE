"""The public Ask route accepts only matching, currently wearable outfit references."""

import json
import uuid
from collections.abc import Sequence
from datetime import UTC, date, datetime, timedelta

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports.llm import ToolAskAnswer, ToolExecutor, ToolSpec, ToolUse
from app.application.ports.weather import WeatherInfo
from app.bootstrap.container import llm_service
from app.config.settings import Settings
from app.infrastructure.persistence.models.user import User, UserSettings
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.routers import ask as ask_router


class _Weather:
    def __init__(self) -> None:
        self.forecast_calls: list[tuple[str, date]] = []

    async def get_forecast(self, location: str, target_date: date) -> WeatherInfo | None:
        self.forecast_calls.append((location, target_date))
        return WeatherInfo(6, 6, 0.8, "小雨")

    async def get_today(self, location: str) -> WeatherInfo:
        raise AssertionError("A future forecast must never read current conditions")


class _ButlerLlm:
    def __init__(self, scenario: str, coat: WardrobeItem) -> None:
        self.scenario = scenario
        self.coat = coat

    async def ask_with_tools(
        self,
        system_prompt: str,
        question: str,
        tools: Sequence[ToolSpec],
        execute: ToolExecutor,
        *,
        json_mode: bool,
        max_rounds: int,
    ) -> ToolAskAnswer:
        assert json_mode
        assert {tool.name for tool in tools} == {"get_weather", "search_wardrobe", "validate_outfit"}

        weather = await execute("get_weather", {"when": "tomorrow"})
        assert weather.content["kind"] == "daily_forecast"
        assert weather.content["condition"] == "小雨"

        search = await execute("search_wardrobe", {"category": "outerwear", "text": "coat", "season": "autumn"})
        assert len(search.content["items"]) == 1
        coat = search.content["items"][0]
        assert self.coat.title not in json.dumps(coat)
        assert str(self.coat.id) not in json.dumps(coat)
        assert "do not send this storage location" not in system_prompt

        # The initial list has a top, bottom, coat and shoes in that order.
        positions = [1, 2, 4]
        cold = await execute("validate_outfit", {"positions": positions, "when": "tomorrow"})
        assert cold.content == {"valid": False, "missing_categories": ["outerwear"]}
        positions.append(coat["position"])

        used = [ToolUse("get_weather", weather.summary), ToolUse("search_wardrobe", search.summary)]
        if self.scenario != "unchecked":
            checked = await execute("validate_outfit", {"positions": positions, "when": "tomorrow"})
            assert checked.content["valid"] is True
            used.append(ToolUse("validate_outfit", checked.summary))
        if self.scenario == "mismatch":
            positions.append(99)
        if self.scenario == "became_dirty":
            self.coat.status = "dirty"

        return ToolAskAnswer(
            text=json.dumps(
                {
                    "answer": "明天小雨，穿衬衫、长裤、夹克和靴子。",
                    "reply_kind": "outfit",
                    "referenced_items": positions,
                },
                ensure_ascii=False,
            ),
            used=used,
            tokens_used=123,
            rounds=3,
        )


@pytest.mark.parametrize("scenario", ["approved", "unchecked", "mismatch", "became_dirty"])
async def test_ask_butler_releases_only_the_checked_outfit(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch, scenario: str
) -> None:
    owner = User(id=uuid.uuid4())
    other = User(id=uuid.uuid4())
    db_session.add_all([owner, other])
    await db_session.flush()
    db_session.add(UserSettings(user_id=owner.id, timezone="UTC", location="Shanghai"))
    # Only the coat with matching title, ownership, availability and season should be returned.
    items = [
        WardrobeItem(user_id=owner.id, title=category, category=category, color_main="black")
        for category in ("top", "bottom", "shoes")
    ]
    coat = WardrobeItem(
        user_id=owner.id,
        title="Private Coat Title",
        category="outerwear",
        color_main="blue",
        location="do not send this storage location",
    )
    items.append(coat)
    db_session.add_all(items)
    for user, status, seasons, deleted in (
        (other, "wearable", None, None),
        (owner, "dirty", None, None),
        (owner, "wearable", {"tags": ["summer"]}, None),
        (owner, "wearable", None, datetime.now(UTC)),
    ):
        db_session.add(
            WardrobeItem(
                user_id=user.id,
                title="Other coat",
                category="outerwear",
                color_main="brown",
                status=status,
                season_tags=seasons,
                deleted_at=deleted,
            )
        )
    await db_session.flush()

    settings = Settings(
        APP_ENV="dev", ENABLE_ASK_AIDROBE=True, ENABLE_ASK_TOOLS=True, ENABLE_ASK_STRUCTURED_OUTPUT=True
    )
    monkeypatch.setattr(ask_router, "get_settings", lambda: settings)
    monkeypatch.setattr(ask_router.ask_uc, "get_settings", lambda: settings)
    weather = _Weather()

    app = FastAPI()
    app.dependency_overrides[get_db_session] = lambda: db_session
    app.dependency_overrides[llm_service] = lambda: _ButlerLlm(scenario, coat)
    app.dependency_overrides[ask_router._ask_weather_service] = lambda: weather
    app.include_router(ask_router.router)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        resp = await client.post(
            "/v1/ask", headers={"X-User-Id": str(owner.id)}, json={"question": "明天下雨怎么穿？"}
        )

    assert resp.status_code == 200
    [(location, forecast_date)] = weather.forecast_calls
    assert location == "Shanghai"
    assert forecast_date == datetime.now(UTC).date() + timedelta(days=1)
    data = resp.json()
    assert data["tokens_used"] == 123
    assert data["used"][0]["tool"] == "get_weather"
    if scenario == "approved":
        assert set(data["item_refs"]) == {str(item.id) for item in items}
        assert {snap["item_id"] for snap in data["item_ref_snapshots"]} == set(data["item_refs"])
        assert data["answer"].startswith("明天小雨")
    else:
        assert data["item_refs"] == data["item_ref_snapshots"] == []
        assert "没能核实" in data["answer"]
