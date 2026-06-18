from typing import Any

import httpx
import structlog

from app.application.ports.llm import LlmAnswer
from app.config.settings import Settings

logger = structlog.get_logger(__name__)


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
            "temperature": 0.4,
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
