import asyncio
import json
from collections.abc import Sequence
from typing import Any

import httpx
import structlog

from app.application.ports.llm import LlmAnswer, ToolAskAnswer, ToolExecutor, ToolSpec, ToolUse
from app.config.settings import Settings

logger = structlog.get_logger(__name__)
_MAX_TOOL_CALLS_PER_ROUND = 4


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
        max_rounds: int = 4,
    ) -> ToolAskAnswer:
        """OpenAI-compatible tool loop. The model may call tools for up to
        `max_rounds - 1` rounds. The final round sets tool_choice=none.
        The turn deadline includes provider requests and tool execution."""
        if not self.settings.QWEN_API_KEY:
            return ToolAskAnswer(text="", tokens_used=0)
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": question},
        ]
        tool_defs = [
            {
                "type": "function",
                "function": {"name": t.name, "description": t.description, "parameters": dict(t.parameters)},
            }
            for t in tools
        ]
        headers = {"Authorization": f"Bearer {self.settings.QWEN_API_KEY}"}
        used: list[ToolUse] = []
        tokens = 0
        rounds = 0
        try:
            async with (
                asyncio.timeout(self.settings.ASK_TOOL_TOTAL_TIMEOUT_SECONDS),
                httpx.AsyncClient(timeout=self.settings.LLM_ASK_TIMEOUT_SECONDS) as client,
            ):
                while True:
                    rounds += 1
                    final = rounds >= max_rounds or not tool_defs
                    payload: dict[str, Any] = {
                        "model": self.settings.QWEN_LLM_MODEL,
                        "messages": messages,
                        "temperature": 0.8,
                        **_thinking_off(self.settings.QWEN_LLM_MODEL),
                    }
                    if tool_defs:
                        payload["tools"] = tool_defs
                        payload["tool_choice"] = "none" if final else "auto"
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
                            text=str(message.get("content") or ""), tokens_used=tokens, used=used, rounds=rounds
                        )
                    calls = calls[:_MAX_TOOL_CALLS_PER_ROUND]
                    messages.append(
                        {"role": "assistant", "content": message.get("content") or "", "tool_calls": calls}
                    )
                    for call in calls:
                        function = call.get("function") or {}
                        name = str(function.get("name") or "")
                        try:
                            arguments = json.loads(function.get("arguments") or "{}")
                        except ValueError:
                            arguments = {}
                        result = await execute(name, arguments if isinstance(arguments, dict) else {})
                        if result.summary:
                            used.append(ToolUse(tool=name, summary=result.summary))
                        messages.append(
                            {
                                "role": "tool",
                                "tool_call_id": call.get("id", ""),
                                "content": json.dumps(result.content, ensure_ascii=False, default=str),
                            }
                        )
        except Exception as exc:
            logger.warning("qwen_llm_ask_with_tools_failed", error=repr(exc), rounds=rounds)
            return ToolAskAnswer(text="", tokens_used=tokens, used=used, rounds=rounds)


def _extract_total_tokens(data: dict[str, Any]) -> int:
    usage = data.get("usage")
    if isinstance(usage, dict):
        total = usage.get("total_tokens")
        if isinstance(total, int):
            return total
    return 0
