from typing import Any

import httpx
import structlog

from app.application.ports.llm import LlmAnswer
from app.config.settings import Settings

logger = structlog.get_logger(__name__)


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


def _extract_total_tokens(data: dict[str, Any]) -> int:
    usage = data.get("usage")
    if isinstance(usage, dict):
        total = usage.get("total_tokens")
        if isinstance(total, int):
            return total
    return 0
