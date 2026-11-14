import base64
import json
from dataclasses import replace
from typing import Any, cast

import httpx

from app.application.ports.vision import StyleAnalysis, VisionResult
from app.common.locale import DEFAULT_LOCALE, resolve_locale
from app.config.settings import Settings

# Human-readable language names per locale for the prompt instruction. zh-CN
# MUST yield natural Simplified Chinese; we never want an English display_name
# leaking through for a Chinese user unless the model truly can't name it.
_LOCALE_LANGUAGE: dict[str, str] = {
    "zh-CN": "Simplified Chinese (简体中文)",
    "en-US": "English",
}

# Style descriptors we steer the model toward — these overlap the recommendation
# template rules (`style_context._TEMPLATE_RULES`) so a learned tag can actually
# select a template instead of falling through to the generic default. The model
# may add others when clearly warranted; this is a bias, not a hard whitelist.
_STYLE_VOCABULARY = (
    "minimal, clean, monochrome, formal, professional, office, suit, tailored, "
    "casual, street, sporty, denim, feminine, soft, floral, pastel, classic, "
    "elegant, edgy, vintage, bohemian, oversized"
)
# Defensive bounds on model output before it is persisted / reaches a prompt.
_MAX_TAGS = 8
_MAX_TAG_LEN = 24

# Garment attributes the recommender ranks on. The model answers formality on a
# 1–5 ladder (more reliable than a 0–100 guess); it is stored on the 0–100
# WardrobeItem scale. Anything outside these vocabularies is dropped as unknown.
_FORMALITY_LADDER = {1: 10, 2: 30, 3: 50, 4: 70, 5: 90}
_SEASONS = ("spring", "summer", "autumn", "winter")
_MATERIALS = (
    "cotton", "denim", "linen", "wool", "cashmere", "knit", "silk", "leather",
    "suede", "polyester", "nylon", "down", "fleece", "canvas", "synthetic",
)
_MAX_ITEM_STYLE_TAGS = 4
_ATTRIBUTE_PROMPT = (
    "Also return: formality — an integer 1 to 5 (1 sportswear or loungewear, "
    "2 casual, 3 smart casual, 4 business, 5 formal or evening); seasons — an array "
    "drawn from spring, summer, autumn, winter (use [\"all_season\"] when it suits "
    "every season); material — one word from " + ", ".join(_MATERIALS) + ", or null if "
    "unclear; style_tags — up to 4 words from: " + _STYLE_VOCABULARY + ". "
    "Judge only what is visible; use null or [] rather than guessing."
)


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
        "confidence_score must be a number from 0 to 1. "
        + _ATTRIBUTE_PROMPT
    )


def build_outfit_vision_prompt(locale: str) -> str:
    resolved = resolve_locale(locale)
    language = _LOCALE_LANGUAGE.get(resolved, "Simplified Chinese (简体中文)")
    return (
        "Analyze this outfit photo and identify every distinct wearable garment. "
        "Return only valid JSON with one key, items, whose value is an array of objects. "
        "Each object must contain canonical_name, display_name, category, color_main, "
        "and confidence_score. Do not merge a top and bottom into one item. A pair "
        "(shoes, socks, gloves) is ONE item: never list the left and right one "
        "separately. Exclude the "
        "person, body parts, background, and non-wearable objects. Return at most 12 items. "
        "category must be one of top, bottom, outerwear, dress, shoes, accessory, other "
        "(stable English snake_case; never translate it). color_main must be a stable "
        "English color word. canonical_name must be a short English name. "
        f"display_name MUST be a natural {language} name for the garment. "
        "confidence_score must be a number from 0 to 1. "
        + _ATTRIBUTE_PROMPT
        + " Give these per garment, plus box — [x_min, y_min, x_max, y_max] around that "
        "garment as fractions (0 to 1) of the image width and height. "
        "If no garment is visible, return {\"items\": []}."
    )


def build_style_prompt() -> str:
    # Tags must be stable English (the recommendation template rules key on
    # English words), so — unlike the wardrobe prompt — this one is locale-free.
    return (
        "You are a fashion stylist. Analyze the overall STYLE aesthetic of this "
        "outfit / editorial image (the vibe of the whole look, not individual "
        "garments). Return only valid JSON with keys: style_tags, color_palette, "
        "avoid_tags, confidence.\n"
        "- style_tags: 3-6 lowercase English style descriptors capturing the "
        f"aesthetic. Strongly prefer these words when they apply: {_STYLE_VOCABULARY}. "
        "Add others only when clearly warranted.\n"
        "- color_palette: 2-5 dominant lowercase English color words "
        "(e.g. black, white, navy, beige, camel, pink).\n"
        "- avoid_tags: only descriptors the look ACTIVELY rejects (usually 0-2). "
        "Leave it as [] if unsure; do NOT list every word you did not choose.\n"
        "- confidence: a number from 0 to 1.\n"
        "All words must be stable lowercase English; never translate."
    )


def _bound_tags(raw: Any, *, limit: int = _MAX_TAGS) -> list[str]:
    """Normalize a model-returned tag list: lowercase, trim, de-dupe, cap count
    and per-tag length. Tolerant of a scalar or None."""
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    seen: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            continue
        tag = item.strip().lower()[:_MAX_TAG_LEN]
        if tag and tag not in seen:
            seen.append(tag)
        if len(seen) >= limit:
            break
    return seen


class QwenVision:
    def __init__(self, settings: Settings) -> None:
        if not settings.QWEN_API_KEY:
            raise ValueError("QWEN_API_KEY is required when VISION_PROVIDER=qwen")
        self.settings = settings

    async def analyze_image(self, image_url: str, locale: str = DEFAULT_LOCALE) -> VisionResult:
        resolved = resolve_locale(locale)
        content = await self._request_content(image_url, build_vision_prompt(resolved))
        data = self._parse_json_content(content)
        return self._vision_result(data, resolved)

    async def analyze_outfit(self, image_url: str, locale: str = DEFAULT_LOCALE) -> list[VisionResult]:
        resolved = resolve_locale(locale)
        content = await self._request_content(image_url, build_outfit_vision_prompt(resolved))
        payload = cast(dict[str, Any], json.loads(self._strip_json_fence(content)))
        raw_items = payload.get("items")
        if not isinstance(raw_items, list) or not raw_items:
            raise ValueError("Outfit response contains no garments")
        if len(raw_items) > 12:
            raise ValueError("Outfit response contains too many garments")
        return merge_paired_garments(
            [
                replace(
                    self._vision_result(self._validate_vision_data(raw), resolved),
                    bbox=garment_box(raw.get("box")),
                )
                for raw in raw_items
            ]
        )

    async def analyze_style(self, image_url: str, locale: str = DEFAULT_LOCALE) -> StyleAnalysis:
        # `locale` is accepted for interface parity but ignored: style tags must
        # be stable English so the recommendation template matcher can key on
        # them (same reason category/color stay English in analyze_image).
        image_payload_url = await self._to_data_uri(image_url)
        payload = {
            "model": self.settings.QWEN_VISION_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": build_style_prompt()},
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
        data = cast(dict[str, Any], json.loads(self._strip_json_fence(content)))
        style_tags = _bound_tags(data.get("style_tags"))
        if not style_tags:
            raise ValueError("Style response missing style_tags")
        try:
            confidence = max(0.0, min(1.0, float(data.get("confidence", 0.5))))
        except (TypeError, ValueError):
            confidence = 0.5
        return StyleAnalysis(
            style_tags=style_tags,
            color_palette=_bound_tags(data.get("color_palette")),
            avoid_tags=_bound_tags(data.get("avoid_tags")),
            confidence=confidence,
        )

    async def _request_content(self, image_url: str, prompt: str) -> str:
        # Inline private wardrobe images instead of exposing signed URLs to the
        # third-party vision provider.
        image_payload_url = await self._to_data_uri(image_url)
        payload = {
            "model": self.settings.QWEN_VISION_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
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
        return str(response.json()["choices"][0]["message"]["content"])

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

    @staticmethod
    def _strip_json_fence(content: str) -> str:
        text = content.strip()
        if text.startswith("```"):
            text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return text

    def _parse_json_content(self, content: str) -> dict[str, Any]:
        return self._validate_vision_data(json.loads(self._strip_json_fence(content)))

    @staticmethod
    def _validate_vision_data(raw: Any) -> dict[str, Any]:
        if not isinstance(raw, dict):
            raise ValueError("Vision response item must be an object")
        data = cast(dict[str, Any], raw)
        for key in ("category", "color_main", "confidence_score"):
            if key not in data:
                raise ValueError(f"Vision response missing {key}")
        if not (data.get("canonical_name") or data.get("display_name") or data.get("title")):
            raise ValueError("Vision response missing a name field")
        return data

    @staticmethod
    def _vision_result(data: dict[str, Any], locale: str) -> VisionResult:
        # Lenient on naming keys for older model variants; category/color remain
        # required and stable English fields.
        canonical = str(data.get("canonical_name") or data.get("title") or "")[:120]
        display = str(data.get("display_name") or data.get("title") or canonical)[:120]
        return VisionResult(
            title=display,
            category=str(data["category"])[:32],
            color_main=str(data["color_main"])[:32],
            confidence_score=max(0.0, min(1.0, float(data["confidence_score"]))),
            canonical_name=canonical,
            display_name=display,
            localized_names={locale: display} if display else {},
            **garment_attributes(data),
        )


def garment_attributes(data: dict[str, Any]) -> dict[str, Any]:
    """Normalize the optional attribute keys of one model answer.

    Lenient by design: a missing, malformed or out-of-vocabulary value becomes
    "unknown" instead of failing the import — naming and category are what the
    import cannot do without, these only sharpen ranking.
    """
    formality: int | None = None
    raw_formality = data.get("formality")
    if isinstance(raw_formality, int | float) and not isinstance(raw_formality, bool):
        formality = _FORMALITY_LADDER.get(round(raw_formality))

    raw_seasons = data.get("seasons")
    seasons: list[str] = []
    if isinstance(raw_seasons, list):
        words = {str(w).strip().lower() for w in raw_seasons}
        if "all_season" in words or set(_SEASONS) <= words:
            seasons = ["all_season"]
        else:
            seasons = [season for season in _SEASONS if season in words]

    raw_material = data.get("material")
    material = str(raw_material).strip().lower() if isinstance(raw_material, str) else None
    if material not in _MATERIALS:
        material = None

    raw_styles = data.get("style_tags")
    styles: list[str] = []
    if isinstance(raw_styles, list):
        for word in raw_styles:
            tag = str(word).strip().lower()[:_MAX_TAG_LEN]
            if tag and tag.replace(" ", "").isalpha() and tag not in styles:
                styles.append(tag)
            if len(styles) == _MAX_ITEM_STYLE_TAGS:
                break

    return {"formality_score": formality, "season_tags": seasons, "material": material, "style_tags": styles}


_MIN_BOX_SIDE = 0.05


# Categories worn as a pair. Vision models often report each foot separately;
# confirming both would put two pairs of the same shoes in the wardrobe.
_PAIRED_CATEGORIES = {"shoes"}


def merge_paired_garments(results: list[VisionResult]) -> list[VisionResult]:
    """Collapse left/right halves of one pair into a single candidate.

    Two shoes with the same name and colour are one pair: keep the first, take
    the higher confidence and the union of both boxes. Different shoes (other
    name or colour) stay separate.
    """
    merged: list[VisionResult] = []
    index: dict[tuple[str, str, str], int] = {}
    for result in results:
        if result.category not in _PAIRED_CATEGORIES:
            merged.append(result)
            continue
        key = (
            result.category,
            (result.canonical_name or result.title).strip().lower(),
            result.color_main.strip().lower(),
        )
        if key not in index:
            index[key] = len(merged)
            merged.append(result)
            continue
        first = merged[index[key]]
        box = first.bbox
        if first.bbox and result.bbox:
            box = (
                min(first.bbox[0], result.bbox[0]),
                min(first.bbox[1], result.bbox[1]),
                max(first.bbox[2], result.bbox[2]),
                max(first.bbox[3], result.bbox[3]),
            )
        merged[index[key]] = replace(
            first,
            bbox=box or result.bbox,
            confidence_score=max(first.confidence_score, result.confidence_score),
        )
    return merged


def garment_box(raw: Any) -> tuple[float, float, float, float] | None:
    """A model-reported garment box as clamped fractions, or None if unusable.

    Rejects malformed, inverted or tiny boxes (under 5% of a side) — a bad box
    must leave the candidate on the full photo, never crop to the wrong place.
    """
    if not isinstance(raw, list) or len(raw) != 4:
        return None
    if not all(isinstance(v, int | float) and not isinstance(v, bool) for v in raw):
        return None
    x1, y1, x2, y2 = (max(0.0, min(1.0, float(v))) for v in raw)
    if x2 - x1 < _MIN_BOX_SIDE or y2 - y1 < _MIN_BOX_SIDE:
        return None
    return (x1, y1, x2, y2)
