"""Butler P0: the Qwen tool-calling loop and the tool registry."""

import json
from typing import Any

import httpx

from app.application.ask.tools import AskTool, ToolRegistry
from app.application.ports.llm import ToolResult, ToolSpec
from app.config.settings import Settings
from app.infrastructure.llm.qwen_llm import QwenLlm

WEATHER = ToolSpec(
    name="get_weather",
    description="Weather for a day",
    parameters={"type": "object", "properties": {"day": {"type": "string"}}, "required": ["day"]},
)


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


async def test_registry_reports_unknown_and_failing_tools_to_the_model() -> None:
    async def boom(args: dict[str, Any]) -> ToolResult:
        raise RuntimeError("db down")

    registry = ToolRegistry([AskTool(spec=WEATHER, handler=boom)])

    assert (await registry.execute("nope", {})).content == {"error": "unknown tool nope"}
    assert (await registry.execute("get_weather", {})).content == {"error": "tool failed"}
    assert [s.name for s in registry.specs] == ["get_weather"]
    assert not ToolRegistry()
