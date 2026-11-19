import asyncio
import json
from collections.abc import Sequence
from time import monotonic
from typing import Any, NoReturn

import httpx
import structlog

from app.application.ports.llm import LlmAnswer, ToolAskAnswer, ToolExecutor, ToolResult, ToolSpec, ToolTrace, ToolUse
from app.config.settings import Settings

logger = structlog.get_logger(__name__)
_MAX_TOOL_CALLS_PER_ROUND = 4
_MAX_TRACE_TEXT = 12_000


def _trace_payload(value: Any) -> dict[str, Any]:
    payload = value if isinstance(value, dict) else {"value": value}
    try:
        encoded = json.dumps(payload, ensure_ascii=False, default=str, allow_nan=False)
    except ValueError:
        return {"error": "tool payload is not finite JSON"}
    if len(encoded) > _MAX_TRACE_TEXT:
        return {"truncated": True, "preview": encoded[:_MAX_TRACE_TEXT]}
    return dict(json.loads(encoded))


def _reject_json_constant(value: str) -> NoReturn:
    raise ValueError(f"Invalid JSON constant: {value}")


def _thinking_off(model: str) -> dict[str, Any]:
    """Qwen3-series models think by default: a 15-character reason took 3.2 s
    (150 reasoning tokens) on qwen3.8-flash against the 3 s polish timeout,
    0.7 s with thinking off. Only sent to qwen3* so older models that don't
    know the parameter are unaffected."""
    return {"enable_thinking": False} if model.startswith("qwen3") else {}


class QwenLlm:
    def __init__(self, settings: Settings) -> None:
        if not settings.QWEN_API_KEY:
            raise ValueError("QWEN_API_KEY is required when LLM_PROVIDER=qwen")
        self.settings = settings

    async def polish_reason(self, prompt: str) -> str:
        payload = {
            "model": self.settings.QWEN_LLM_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.4,
            **_thinking_off(self.settings.QWEN_LLM_MODEL),
        }
        headers = {"Authorization": f"Bearer {self.settings.QWEN_API_KEY}"}
        async with httpx.AsyncClient(timeout=self.settings.LLM_TIMEOUT_SECONDS) as client:
            response = await client.post(
                self.settings.QWEN_LLM_BASE_URL,
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
        return str(response.json()["choices"][0]["message"]["content"])

    async def ask(
        self, system_prompt: str, question: str, *, json_mode: bool = False
    ) -> LlmAnswer:
        if not self.settings.QWEN_API_KEY:
            return LlmAnswer(text="", tokens_used=0)

        payload: dict[str, Any] = {
            "model": self.settings.QWEN_LLM_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": question},
            ],
            # 0.4 made repeated asks over the same wardrobe nearly verbatim
            # (user report 2026-07-24); ask is a creative surface, so sample
            # warmer. polish_reason stays at 0.4 — it rewrites fixed facts.
            "temperature": 0.8,
            **_thinking_off(self.settings.QWEN_LLM_MODEL),
        }
        if json_mode:
            # OpenAI-compatible JSON mode — constrain output to a JSON object.
            payload["response_format"] = {"type": "json_object"}
        headers = {"Authorization": f"Bearer {self.settings.QWEN_API_KEY}"}
        try:
            async with httpx.AsyncClient(
                timeout=self.settings.LLM_ASK_TIMEOUT_SECONDS
            ) as client:
                response = await client.post(
                    self.settings.QWEN_LLM_BASE_URL,
                    headers=headers,
                    json=payload,
                )
                response.raise_for_status()
            data = response.json()
            text = str(data["choices"][0]["message"]["content"])
            return LlmAnswer(text=text, tokens_used=_extract_total_tokens(data))
        except Exception as exc:
            logger.warning("qwen_llm_ask_failed", error=str(exc))
            return LlmAnswer(text="", tokens_used=0)


    async def ask_with_tools(
        self,
        system_prompt: str,
        question: str,
        tools: Sequence[ToolSpec],
        execute: ToolExecutor,
        *,
        json_mode: bool = False,
        max_rounds: int = 5,
    ) -> ToolAskAnswer:
        """OpenAI-compatible tool loop. The model may call tools for up to
        `max_rounds - 1` rounds, with one repair round for a rejected design.
        The final round sets tool_choice=none. After design verification it
        has its own provider deadline."""
        if not self.settings.QWEN_API_KEY:
            return ToolAskAnswer(text="", tokens_used=0)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question},
        ]
        tool_defs: list[dict[str, Any]] = [
            {
                "type": "function",
                "function": {"name": t.name, "description": t.description, "parameters": dict(t.parameters)},
            }
            for t in tools
        ]
        setup_tool = next((tool.name for tool in tools if tool.stage == "setup"), None)
        verification_tool = next((tool.name for tool in tools if tool.stage == "verification"), None)
        setup_done = setup_tool is None
        verified = False
        headers = {"Authorization": f"Bearer {self.settings.QWEN_API_KEY}"}
        used: list[ToolUse] = []
        trace: list[ToolTrace] = []
        tokens = 0
        rounds = 0
        round_limit = max_rounds
        phase = "provider"
        try:
            async with (
                asyncio.timeout(self.settings.ASK_TOOL_TOTAL_TIMEOUT_SECONDS) as deadline,
                httpx.AsyncClient(timeout=self.settings.LLM_ASK_TIMEOUT_SECONDS) as client,
            ):
                while True:
                    rounds += 1
                    final = rounds >= round_limit or not tool_defs or verified
                    if verified:
                        deadline.reschedule(asyncio.get_running_loop().time() + self.settings.LLM_ASK_TIMEOUT_SECONDS)
                    required_tool = None
                    if not final:
                        if not setup_done:
                            required_tool = setup_tool
                        elif rounds == round_limit - 1:
                            required_tool = verification_tool
                    payload: dict[str, Any] = {
                        "model": self.settings.QWEN_LLM_MODEL,
                        "messages": messages,
                        "temperature": 0.8,
                        **_thinking_off(self.settings.QWEN_LLM_MODEL),
                    }
                    if tool_defs:
                        payload["tools"] = (
                            [tool for tool in tool_defs if tool["function"]["name"] == required_tool]
                            if required_tool is not None else tool_defs
                        )
                        if final:
                            payload["tool_choice"] = "none"
                        elif required_tool is not None and required_tool == verification_tool:
                            payload["tool_choice"] = {"type": "function", "function": {"name": required_tool}}
                        else:
                            payload["tool_choice"] = "auto"
                    if json_mode:
                        payload["response_format"] = {"type": "json_object"}
                    response = await client.post(self.settings.QWEN_LLM_BASE_URL, headers=headers, json=payload)
                    response.raise_for_status()
                    data = response.json()
                    tokens += _extract_total_tokens(data)
                    message = data["choices"][0]["message"]
                    calls = message.get("tool_calls") or []
                    if final or not calls:
                        return ToolAskAnswer(
                            text=str(message.get("content") or ""), tokens_used=tokens, used=used,
                            rounds=rounds, tool_trace=trace,
                        )
                    calls = calls[:_MAX_TOOL_CALLS_PER_ROUND]
                    messages.append(
                        {"role": "assistant", "content": message.get("content") or "", "tool_calls": calls}
                    )
                    validation_failed = False
                    for call in calls:
                        function = call.get("function") or {}
                        name = str(function.get("name") or "")
                        started = monotonic()
                        result = ToolResult({"error": "tool execution interrupted"}, "")
                        try:
                            arguments = json.loads(
                                function.get("arguments") or "{}", parse_constant=_reject_json_constant,
                            )
                        except (TypeError, ValueError):
                            arguments = None
                        try:
                            if not isinstance(arguments, dict):
                                result = ToolResult({"error": "tool arguments must be a JSON object"}, "")
                            elif required_tool is not None and name != required_tool:
                                result = ToolResult({"error": f"only {required_tool} is available now"}, "")
                            else:
                                phase = "tool"
                                result = await execute(name, arguments)
                        finally:
                            content = _trace_payload(result.content)
                            failed = bool(content.get("error")) or (
                                isinstance(result.content, dict) and result.content.get("valid") is False
                            )
                            trace.append(ToolTrace(
                                tool=name[:80], arguments=_trace_payload(arguments), result=content,
                                status="error" if failed else "success",
                                duration_ms=max(0, round((monotonic() - started) * 1000)),
                            ))
                        if not failed and isinstance(result.content, dict):
                            if name == setup_tool and result.content.get("updated") is True:
                                setup_done = True
                                verified = False
                            elif name == verification_tool and result.content.get("valid") is True:
                                verified = True
                        elif name == verification_tool and isinstance(result.content, dict):
                            validation_failed |= result.content.get("valid") is False
                        if result.summary and not failed:
                            used.append(ToolUse(tool=name, summary=result.summary))
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.get("id", ""),
                                "content": json.dumps(result.content, ensure_ascii=False, default=str, allow_nan=False),
                            }
                        )
                        phase = "provider"
                    if validation_failed and not verified and rounds == max_rounds - 1:
                        round_limit += 1
        except Exception as exc:
            logger.warning("qwen_llm_ask_with_tools_failed", error_type=type(exc).__name__, rounds=rounds)
            reason = "provider_error" if phase == "provider" else "tool_error"
            if isinstance(exc, TimeoutError):
                reason = "deadline_exceeded"
            elif isinstance(exc, httpx.TimeoutException):
                reason = "provider_timeout" if phase == "provider" else "tool_timeout"
            return ToolAskAnswer(
                text="", tokens_used=tokens, used=used, rounds=rounds, tool_trace=trace, failure_reason=reason,
            )


def _extract_total_tokens(data: dict[str, Any]) -> int:
    usage = data.get("usage")
    if isinstance(usage, dict):
        total = usage.get("total_tokens")
        if isinstance(total, int):
            return total
    return 0
