import json
from typing import Any

import httpx

from app.application.ports.ocr import OcrResult
from app.config.settings import Settings
from app.infrastructure.storage.source_image import image_data_uri, load_source_bytes

OCR_PROMPT = (
    "You are reading a shopping receipt, order screenshot, or purchase-confirmation "
    "email for a single clothing item. Return only valid JSON with keys: "
    "title, category, color_main, size_label, purchase_price, purchase_date, "
    "confidence_score. "
    "category must be one of top, bottom, outerwear, dress, shoes, accessory, other "
    "or null if unknown. "
    "purchase_price must be a plain decimal string (e.g. \"199.00\") or null. "
    "purchase_date must be an ISO-8601 date (YYYY-MM-DD) or null. "
    "Any field other than title may be null when the source does not show it. "
    "confidence_score must be a number from 0 to 1."
)


class QwenOcr:
    """Tongyi Qwen-VL OCR provider. Real implementation, gated behind
    ``OCR_PROVIDER=qwen`` + a configured ``QWEN_API_KEY`` — never required for
    dev/test, which default to :class:`FakeOcr`.
    """

    def __init__(self, settings: Settings) -> None:
        if not settings.QWEN_API_KEY:
            raise ValueError("QWEN_API_KEY is required when OCR_PROVIDER=qwen")
        self.settings = settings

    async def extract_text(self, image_url: str, *, source_key: str | None = None) -> OcrResult:
        # Inline as base64 for the same reason as QwenVision: receipt screenshots
        # live in private storage a remote model cannot reach.
        image_payload_url = await self._to_data_uri(image_url, source_key)
        payload = {
            "model": self.settings.QWEN_OCR_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": OCR_PROMPT},
                        {"type": "image_url", "image_url": {"url": image_payload_url}},
                    ],
                }
            ],
            "response_format": {"type": "json_object"},
        }
        headers = {
            "Authorization": f"Bearer {self.settings.QWEN_API_KEY}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self.settings.OCR_TIMEOUT_SECONDS) as client:
            response = await client.post(
                self.settings.QWEN_OCR_BASE_URL,
                headers=headers,
                json=payload,
            )
            response.raise_for_status()

        content = response.json()["choices"][0]["message"]["content"]
        data = self._parse_json_content(content)
        return OcrResult(
            title=str(data["title"])[:120],
            category=_opt_str(data.get("category"), 32),
            color_main=_opt_str(data.get("color_main"), 32),
            size_label=_opt_str(data.get("size_label"), 32),
            purchase_price=_opt_str(data.get("purchase_price"), 32),
            purchase_date=_opt_str(data.get("purchase_date"), 32),
            confidence_score=max(0.0, min(1.0, float(data.get("confidence_score", 0.0)))),
        )

    async def _to_data_uri(self, image_url: str, source_key: str | None = None) -> str:
        if image_url.startswith("data:"):
            return image_url
        return image_data_uri(await load_source_bytes(image_url, source_key))

    def _parse_json_content(self, content: str) -> dict[str, Any]:
        text = content.strip()
        if text.startswith("```"):
            text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = json.loads(text)
        if not isinstance(data, dict):
            raise ValueError(f"OCR response must be a JSON object, got {type(data).__name__}")
        if "title" not in data:
            raise ValueError("OCR response missing title")
        return data


def _opt_str(value: object, max_len: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:max_len]
