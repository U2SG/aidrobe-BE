import json
from typing import Any, cast

import httpx

from app.application.ports.vision import VisionResult
from app.config.settings import Settings

VISION_PROMPT = (
    "Analyze this wardrobe item image. Return only valid JSON with keys: "
    "title, category, color_main, confidence_score. "
    "category must be one of top, bottom, outerwear, dress, shoes, accessory, other. "
    "confidence_score must be a number from 0 to 1."
)


class QwenVision:
    def __init__(self, settings: Settings) -> None:
        if not settings.QWEN_API_KEY:
            raise ValueError("QWEN_API_KEY is required when VISION_PROVIDER=qwen")
        self.settings = settings

    async def analyze_image(self, image_url: str) -> VisionResult:
        payload = {
            "model": self.settings.QWEN_VISION_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": VISION_PROMPT},
                        {"type": "image_url", "image_url": {"url": image_url}},
                    ],
                }
            ],
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self.settings.QWEN_API_KEY}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self.settings.VISION_TIMEOUT_SECONDS) as client:
            response = await client.post(
                self.settings.QWEN_VISION_BASE_URL,
                headers=headers,
                json=payload,
            )
            response.raise_for_status()

        content = response.json()["choices"][0]["message"]["content"]
        data = self._parse_json_content(content)
        return VisionResult(
            title=str(data["title"])[:120],
            category=str(data["category"])[:32],
            color_main=str(data["color_main"])[:32],
            confidence_score=max(0.0, min(1.0, float(data["confidence_score"]))),
        )

    def _parse_json_content(self, content: str) -> dict[str, Any]:
        text = content.strip()
        if text.startswith("```"):
            text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = cast(dict[str, Any], json.loads(text))
        for key in ("title", "category", "color_main", "confidence_score"):
            if key not in data:
                raise ValueError(f"Vision response missing {key}")
        return data
