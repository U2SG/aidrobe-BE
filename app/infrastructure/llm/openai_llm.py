from typing import Any

import httpx
import structlog

from app.config.settings import Settings

logger = structlog.get_logger(__name__)


class NoOpLlm:
    """Template fallback LLM implementation."""

    async def polish_reason(self, prompt: str) -> str:
        return ""


class OpenAiLlm:
    """OpenAI-backed reason polisher using the Responses API."""

    RESPONSES_URL = "https://api.openai.com/v1/responses"

    def __init__(self, settings: Settings) -> None:
        self.api_key = settings.OPENAI_API_KEY
        self.model = settings.OPENAI_LLM_MODEL
        self.timeout_seconds = settings.LLM_TIMEOUT_SECONDS

    async def polish_reason(self, prompt: str) -> str:
        if not self.api_key:
            return ""

        payload = {
            "model": self.model,
            "instructions": "Rewrite the recommendation reason in concise, natural Chinese. Return only the reason.",
            "input": prompt,
        }
        headers = {"Authorization": f"Bearer {self.api_key}"}

        try:
            async with httpx.AsyncClient(timeout=self.timeout_seconds) as client:
                response = await client.post(self.RESPONSES_URL, headers=headers, json=payload)
                response.raise_for_status()
            return _extract_output_text(response.json()).strip()
        except Exception as exc:
            logger.warning("openai_llm_polish_failed", error=str(exc))
            return ""


def _extract_output_text(data: dict[str, Any]) -> str:
    output_text = data.get("output_text")
    if isinstance(output_text, str):
        return output_text

    chunks: list[str] = []
    output = data.get("output")
    if isinstance(output, list):
        for item in output:
            if not isinstance(item, dict):
                continue
            content = item.get("content")
            if not isinstance(content, list):
                continue
            for content_item in content:
                if isinstance(content_item, dict) and isinstance(content_item.get("text"), str):
                    chunks.append(content_item["text"])
    return "".join(chunks)
