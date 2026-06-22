import base64
import json
from typing import Any, cast

import httpx

from app.application.ports.vision import VisionResult
from app.common.locale import DEFAULT_LOCALE, resolve_locale
from app.config.settings import Settings

# Human-readable language names per locale for the prompt instruction. zh-CN
# MUST yield natural Simplified Chinese; we never want an English display_name
# leaking through for a Chinese user unless the model truly can't name it.
_LOCALE_LANGUAGE: dict[str, str] = {
    "zh-CN": "Simplified Chinese (简体中文)",
    "en-US": "English",
}


def build_vision_prompt(locale: str) -> str:
    resolved = resolve_locale(locale)
    language = _LOCALE_LANGUAGE.get(resolved, "Simplified Chinese (简体中文)")
    return (
        "Analyze this wardrobe item image. Return only valid JSON with keys: "
        "canonical_name, display_name, category, color_main, confidence_score. "
        "category must be one of top, bottom, outerwear, dress, shoes, accessory, other "
        "(stable English snake_case; never translate it). "
        "color_main must be a stable English color word (e.g. black, white, navy). "
        f"canonical_name is a short English name for the item. "
        f"display_name MUST be a natural {language} name for the item — do not "
        f"return English for display_name unless the item genuinely has no "
        f"{language} name. "
        "confidence_score must be a number from 0 to 1."
    )


class QwenVision:
    def __init__(self, settings: Settings) -> None:
        if not settings.QWEN_API_KEY:
            raise ValueError("QWEN_API_KEY is required when VISION_PROVIDER=qwen")
        self.settings = settings

    async def analyze_image(self, image_url: str, locale: str = DEFAULT_LOCALE) -> VisionResult:
        # Inline the image as base64 instead of handing DashScope a URL to fetch:
        # wardrobe images live in private / non-public-internet storage (local
        # minio in dev, private OSS bucket in prod), which a remote model can't
        # reach — and we shouldn't expose signed URLs to a third party anyway.
        resolved = resolve_locale(locale)
        image_payload_url = await self._to_data_uri(image_url)
        payload = {
            "model": self.settings.QWEN_VISION_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": build_vision_prompt(resolved)},
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
        async with httpx.AsyncClient(timeout=self.settings.VISION_TIMEOUT_SECONDS) as client:
            response = await client.post(
                self.settings.QWEN_VISION_BASE_URL,
                headers=headers,
                json=payload,
            )
            response.raise_for_status()

        content = response.json()["choices"][0]["message"]["content"]
        data = self._parse_json_content(content)
        # Lenient on the naming keys: an older model variant may only return
        # `title`. Fall back so a missing display_name/canonical_name never
        # crashes the import — canonical defaults to whatever name we got.
        canonical = str(data.get("canonical_name") or data.get("title") or "")[:120]
        display = str(data.get("display_name") or data.get("title") or canonical)[:120]
        return VisionResult(
            title=display,
            category=str(data["category"])[:32],
            color_main=str(data["color_main"])[:32],
            confidence_score=max(0.0, min(1.0, float(data["confidence_score"]))),
            canonical_name=canonical,
            display_name=display,
            localized_names={resolved: display} if display else {},
        )

    async def _to_data_uri(self, image_url: str) -> str:
        # Already inlined (e.g. a caller passed a data URI) — pass through.
        if image_url.startswith("data:"):
            return image_url
        # Some image hosts 403 a default httpx UA; present as a browser.
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            ),
        }
        async with httpx.AsyncClient(timeout=self.settings.VISION_TIMEOUT_SECONDS, headers=headers) as client:
            response = await client.get(image_url, follow_redirects=True)
            response.raise_for_status()
        content_type = response.headers.get("content-type", "image/jpeg").split(";")[0].strip()
        if not content_type.startswith("image/"):
            content_type = "image/jpeg"
        encoded = base64.b64encode(response.content).decode("ascii")
        return f"data:{content_type};base64,{encoded}"

    def _parse_json_content(self, content: str) -> dict[str, Any]:
        text = content.strip()
        if text.startswith("```"):
            text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        data = cast(dict[str, Any], json.loads(text))
        for key in ("category", "color_main", "confidence_score"):
            if key not in data:
                raise ValueError(f"Vision response missing {key}")
        if not (data.get("canonical_name") or data.get("display_name") or data.get("title")):
            raise ValueError("Vision response missing a name field")
        return data
