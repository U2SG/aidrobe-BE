"""Butler P0: the Qwen tool-calling loop and the tool registry."""

import asyncio
import json
from typing import Any

import httpx
import pytest

from app.application.ask.tools import AskTool, ToolRegistry
from app.application.ports.llm import ToolResult, ToolSpec
from app.config.settings import Settings
from app.infrastructure.llm.qwen_llm import QwenLlm

WEATHER = ToolSpec(
    name="get_weather",
    description="Weather for a day",
    parameters={"type": "object", "properties": {"day": {"type": "string"}}, "required": ["day"]},
)
BRIEF = ToolSpec(
    name="update_design_brief",
    description="Update the current design request",
    parameters={"type": "object"},
    stage="setup",
)
SEARCH = ToolSpec(
    name="search_wardrobe",
    description="Find wardrobe pieces",
    parameters={"type": "object"},
)
VALIDATION = ToolSpec(
    name="validate_outfit",
    description="Check an exact outfit",
    parameters={"type": "object"},
    stage="verification",
)
DESIGN_TOOLS = [BRIEF, WEATHER, SEARCH, VALIDATION]


def _llm() -> QwenLlm:
    return QwenLlm(Settings(LLM_PROVIDER="qwen", QWEN_API_KEY="k", QWEN_LLM_MODEL="qwen3.8-flash"))


def _route(monkeypatch, replies: list[dict[str, Any]], seen: list[dict[str, Any]]) -> None:
    async_client = httpx.AsyncClient

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(json.loads(request.content))
        return httpx.Response(200, request=request, json=replies.pop(0))

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: async_client(transport=httpx.MockTransport(handler)))


def _reply(content: str = "", calls: list[dict[str, Any]] | None = None, tokens: int = 10) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": content}
    if calls:
        message["tool_calls"] = calls
    return {"choices": [{"message": message}], "usage": {"total_tokens": tokens}}


def _call(name: str, args: dict[str, Any], call_id: str = "c1") -> dict[str, Any]:
    return {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def _offered_names(payload: dict[str, Any]) -> set[str]:
    return {tool["function"]["name"] for tool in payload.get("tools", [])}


async def test_tool_call_is_executed_and_fed_back(monkeypatch) -> None:
    seen: list[dict[str, Any]] = []
    _route(
        monkeypatch,
        [_reply(calls=[_call("get_weather", {"day": "tomorrow"})], tokens=40), _reply("穿外套", tokens=60)],
        seen,
    )
    executed: list[tuple[str, dict[str, Any]]] = []

    async def execute(name: str, args: dict[str, Any]) -> ToolResult:
        executed.append((name, args))
        return ToolResult(content={"condition": "小雨", "temperature_c": 14}, summary="看了明天的天气：小雨 14°")

    answer = await _llm().ask_with_tools("sys", "明天穿什么", [WEATHER], execute)

    assert answer.text == "穿外套"
    assert executed == [("get_weather", {"day": "tomorrow"})]
    assert [u.summary for u in answer.used] == ["看了明天的天气：小雨 14°"]
    assert answer.tokens_used == 100 and answer.rounds == 2
    tool_message = seen[1]["messages"][-1]
    assert tool_message["role"] == "tool" and tool_message["tool_call_id"] == "c1"
    assert json.loads(tool_message["content"])["condition"] == "小雨"
    assert seen[0]["enable_thinking"] is False and seen[0]["tool_choice"] == "auto"
    assert answer.tool_trace[0].arguments == {"day": "tomorrow"}
    assert answer.tool_trace[0].result == {"condition": "小雨", "temperature_c": 14}
    assert answer.tool_trace[0].status == "success"
    assert answer.tool_trace[0].duration_ms >= 0


async def test_last_round_forces_an_answer(monkeypatch) -> None:
    seen: list[dict[str, Any]] = []
    loop_call = _reply(calls=[_call("get_weather", {"day": "today"})])
    _route(monkeypatch, [loop_call, loop_call, _reply("好的")], seen)

    async def execute(name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult(content={}, summary="")

    answer = await _llm().ask_with_tools("sys", "q", [WEATHER], execute, max_rounds=3)

    assert answer.text == "好的" and answer.rounds == 3
    assert [p["tool_choice"] for p in seen] == ["auto", "auto", "none"]
    assert answer.used == []  # tools with an empty summary are not surfaced


async def test_provider_error_returns_empty_text(monkeypatch) -> None:
    async_client = httpx.AsyncClient

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, request=request, text="boom")

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: async_client(transport=httpx.MockTransport(handler)))

    async def execute(name: str, args: dict[str, Any]) -> ToolResult:
        raise AssertionError("not reached")

    answer = await _llm().ask_with_tools("sys", "q", [WEATHER], execute)
    assert answer.text == ""
    assert answer.failure_reason == "provider_error"


async def test_registry_reports_unknown_and_failing_tools_to_the_model() -> None:
    async def boom(args: dict[str, Any]) -> ToolResult:
        raise RuntimeError("db down")

    registry = ToolRegistry([AskTool(spec=WEATHER, handler=boom)])

    assert (await registry.execute("nope", {})).content == {"error": "unknown tool nope"}
    assert (await registry.execute("get_weather", {})).content == {"error": "tool failed"}
    assert [s.name for s in registry.specs] == ["get_weather"]
    assert not ToolRegistry()


async def test_default_budget_reserves_a_round_for_design_validation(monkeypatch) -> None:
    names = ["update_design_brief", "get_weather", "search_wardrobe", "validate_outfit"]
    final = json.dumps({"answer": "这套保留亮色上衣。", "reply_kind": "outfit", "referenced_items": [1, 2, 3]})
    seen: list[dict[str, Any]] = []
    _route(monkeypatch, [*[_reply(calls=[_call(name, {}, str(i))]) for i, name in enumerate(names)],
                         _reply(final)], seen)
    executed: list[str] = []

    async def execute(name: str, args: dict[str, Any]) -> ToolResult:
        executed.append(name)
        if name == "update_design_brief":
            return ToolResult({"updated": True}, name)
        if name == "validate_outfit":
            return ToolResult({"valid": True, "positions": [1, 2, 3]}, name)
        return ToolResult({"items": []}, name)

    llm = _llm()
    answer = await llm.ask_with_tools("sys", "按天气调整颜色", DESIGN_TOOLS, execute,
                                      max_rounds=llm.settings.ASK_TOOL_MAX_ROUNDS)
    assert executed == names
    assert answer.text == final and answer.tokens_used == 50
    assert [payload["tool_choice"] for payload in seen] == ["auto", "auto", "auto", "auto", "none"]
    assert _offered_names(seen[0]) == {"update_design_brief"}
    assert _offered_names(seen[1]) == {tool.name for tool in DESIGN_TOOLS}
    assert _offered_names(seen[3]) == {"validate_outfit"}
    assert len(answer.tool_trace) == 4 and answer.failure_reason is None


async def test_design_rejects_validation_before_updating_the_brief(monkeypatch) -> None:
    seen: list[dict[str, Any]] = []
    final = json.dumps({
        "answer": "保留毛衣，换一双鞋。", "reply_kind": "outfit", "referenced_items": [1, 2, 4],
    })
    _route(monkeypatch, [
        _reply(calls=[_call("validate_outfit", {"positions": [1, 2, 3]})]),
        _reply(calls=[_call("update_design_brief", {"change_positions": [3]})]),
        _reply(calls=[_call("validate_outfit", {"positions": [1, 2, 4]})]),
        _reply(final),
    ], seen)
    executed: list[str] = []

    async def execute(name: str, args: dict[str, Any]) -> ToolResult:
        executed.append(name)
        if name == "update_design_brief":
            return ToolResult({"updated": True}, name)
        return ToolResult({"valid": True, "positions": args["positions"]}, name)

    answer = await _llm().ask_with_tools("sys", "根据穿着感受换鞋", DESIGN_TOOLS, execute)

    assert executed == ["update_design_brief", "validate_outfit"]
    assert _offered_names(seen[0]) == _offered_names(seen[1]) == {"update_design_brief"}
    assert answer.tool_trace[0].tool == "validate_outfit"
    assert answer.tool_trace[0].status == "error"
    assert answer.tool_trace[0].result["error"]
    assert seen[-1]["tool_choice"] == "none"
    assert answer.text == final and answer.rounds == 4


async def test_reserved_validation_round_rejects_an_additional_wardrobe_search(monkeypatch) -> None:
    seen: list[dict[str, Any]] = []
    final = json.dumps({
        "answer": "还没核实好新搭配，请再确认鞋子。", "reply_kind": "general", "referenced_items": [],
    })
    _route(monkeypatch, [
        _reply(calls=[_call("update_design_brief", {})]),
        _reply(calls=[_call("get_weather", {"place": "苏州"})]),
        _reply(calls=[_call("search_wardrobe", {"category": "shoes"})]),
        _reply(calls=[_call("search_wardrobe", {"category": "outerwear"})]),
        _reply(final),
    ], seen)
    executed: list[tuple[str, dict[str, Any]]] = []

    async def execute(name: str, args: dict[str, Any]) -> ToolResult:
        executed.append((name, args))
        return ToolResult({"updated": True} if name == "update_design_brief" else {"items": []}, name)

    answer = await _llm().ask_with_tools("sys", "按苏州行程调整", DESIGN_TOOLS, execute)

    assert [name for name, _ in executed] == ["update_design_brief", "get_weather", "search_wardrobe"]
    assert _offered_names(seen[3]) == {"validate_outfit"}
    assert answer.tool_trace[-1].tool == "search_wardrobe"
    assert answer.tool_trace[-1].arguments == {"category": "outerwear"}
    assert answer.tool_trace[-1].status == "error"
    assert answer.tool_trace[-1].result["error"]
    assert json.loads(seen[-1]["messages"][-1]["content"])["error"]
    assert seen[-1]["tool_choice"] == "none"
    assert answer.text == final and answer.rounds == 5


async def test_design_can_ask_a_general_clarification_before_validation(monkeypatch) -> None:
    seen: list[dict[str, Any]] = []
    final = json.dumps({"answer": "这次要换鞋还是换外套？", "reply_kind": "general", "referenced_items": []})
    _route(monkeypatch, [_reply(calls=[_call("update_design_brief", {})]), _reply(final)], seen)
    executed: list[str] = []

    async def execute(name: str, args: dict[str, Any]) -> ToolResult:
        executed.append(name)
        return ToolResult({"updated": True}, "")

    answer = await _llm().ask_with_tools("sys", "想调整得更合适", DESIGN_TOOLS, execute)

    assert executed == ["update_design_brief"]
    assert answer.text == final and answer.rounds == 2
    assert not any(entry.tool == "validate_outfit" for entry in answer.tool_trace)


@pytest.mark.parametrize("arguments", ['{bad json', '[]', 'null', '{"limit":NaN}', '{"limit":Infinity}'])
async def test_invalid_arguments_are_traced_without_executing_defaults(monkeypatch, arguments: str) -> None:
    seen: list[dict[str, Any]] = []
    call = _call("get_weather", {})
    call["function"]["arguments"] = arguments
    _route(monkeypatch, [_reply(calls=[call]), _reply("请确认城市")], seen)

    async def execute(name: str, args: dict[str, Any]) -> ToolResult:
        raise AssertionError("invalid arguments must not execute a default lookup")

    answer = await _llm().ask_with_tools("sys", "q", [WEATHER], execute)
    assert answer.used == []
    assert answer.tool_trace[0].status == "error"
    assert answer.tool_trace[0].result == {"error": "tool arguments must be a JSON object"}
    assert json.loads(seen[1]["messages"][-1]["content"])["error"]


async def test_invalid_outfit_without_error_text_is_still_a_failed_trace(monkeypatch) -> None:
    seen: list[dict[str, Any]] = []
    _route(monkeypatch, [_reply(calls=[_call("validate_outfit", {"positions": [1, 2]})]), _reply("还缺鞋")], seen)

    async def execute(name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult({"valid": False, "missing_categories": ["shoes"]}, "")

    answer = await _llm().ask_with_tools("sys", "q", [WEATHER], execute)
    assert answer.used == []
    assert answer.tool_trace[0].status == "error"
    assert answer.tool_trace[0].result["missing_categories"] == ["shoes"]


async def test_tool_deadline_preserves_interrupted_execution(monkeypatch) -> None:
    seen: list[dict[str, Any]] = []
    _route(monkeypatch, [_reply(calls=[_call("get_weather", {"day": "today"})])], seen)

    async def execute(name: str, args: dict[str, Any]) -> ToolResult:
        await asyncio.Event().wait()
        raise AssertionError("deadline should interrupt the tool")

    llm = _llm()
    llm.settings.ASK_TOOL_TOTAL_TIMEOUT_SECONDS = 0.02
    answer = await llm.ask_with_tools("sys", "q", [WEATHER], execute)
    assert answer.text == "" and answer.used == []
    assert answer.failure_reason == "deadline_exceeded"
    assert answer.tool_trace[0].status == "error"
    assert answer.tool_trace[0].result == {"error": "tool execution interrupted"}
    assert answer.tokens_used == 10


async def test_trace_is_bounded_without_losing_failed_status(monkeypatch) -> None:
    seen: list[dict[str, Any]] = []
    _route(monkeypatch, [_reply(calls=[_call("validate_outfit", {})]), _reply("无法核实")], seen)

    async def execute(name: str, args: dict[str, Any]) -> ToolResult:
        return ToolResult({"valid": False, "detail": "x" * 20_000}, "")

    answer = await _llm().ask_with_tools("sys", "q", [WEATHER], execute)
    assert answer.tool_trace[0].status == "error"
    assert answer.tool_trace[0].result["truncated"] is True
    assert len(answer.tool_trace[0].result["preview"]) == 12_000
