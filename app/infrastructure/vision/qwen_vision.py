import base64
import json
import math
from dataclasses import replace
from typing import Any, cast

import httpx

from app.application.ports.vision import StyleAnalysis, VisionResult
from app.common.locale import DEFAULT_LOCALE, resolve_locale
from app.config.settings import Settings
from app.infrastructure.storage.source_image import image_data_uri, load_source_bytes

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
        "If no garment is visible, return {\"no_garment\": true}. "
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
        + " Give these per garment, plus box_2d — [left, top, right, bottom] as integers "
        "on a 0-to-1000 grid (left/top are 0, right/bottom are 1000). "
        "Use the tight rectangle around the visible fabric of that item, not the body or outfit. "
        "For shorts the bottom edge is the shorts hem, never the knee, lower leg or foot. "
        "For shoes include both visible shoes, stopping at the shoe opening. For layered garments "
        "include only the specific layer. Use null if the item cannot be separately located. "
        "Omit an inner layer when only a narrow strip at the hem or collar is visible. "
        "A strip of fabric is insufficient to determine whether a separate garment exists, "
        "or to call it a T-shirt, tank or blouse. If an uncertain layer is returned, set style_unverified=true. "
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


class NoGarmentsDetected(ValueError):
    pass


class QwenVision:
    def __init__(self, settings: Settings) -> None:
        if not settings.QWEN_API_KEY:
            raise ValueError("QWEN_API_KEY is required when VISION_PROVIDER=qwen")
        self.settings = settings

    async def analyze_image(
        self, image_url: str, locale: str = DEFAULT_LOCALE, *, source_key: str | None = None
    ) -> VisionResult:
        resolved = resolve_locale(locale)
        content = await self._request_content(image_url, build_vision_prompt(resolved), source_key=source_key)
        data = self._parse_json_content(content)
        return self._vision_result(data, resolved)

    async def analyze_outfit(self, image_url: str, locale: str = DEFAULT_LOCALE) -> list[VisionResult]:
        resolved = resolve_locale(locale)
        content = await self._request_content(image_url, build_outfit_vision_prompt(resolved))
        payload = cast(dict[str, Any], json.loads(self._strip_json_fence(content)))
        raw_items = payload.get("items")
        if not isinstance(raw_items, list):
            raise ValueError("Outfit response missing items")
        if not raw_items:
            return []
        if len(raw_items) > 12:
            raise ValueError("Outfit response contains too many garments")
        return merge_paired_garments(
            [
                replace(
                    self._vision_result(self._validate_vision_data(raw), resolved),
                    bbox=(
                        garment_box(raw["box_2d"], scale=1000)
                        if "box_2d" in raw else garment_box(raw.get("box"))
                    ),
                    style_unverified=raw.get("style_unverified") is True,
                )
                for raw in raw_items
            ]
        )

    async def review_garment_crops(self, crops: list[bytes], garments: list[VisionResult]) -> list[bool]:
        expected = [
            {"item": garment.canonical_name or garment.title, "category": garment.category, "color": garment.color_main}
            for garment in garments
        ]
        content = await self._request_content(
            _image_data_uri(crops[0]),
            "Independently inspect each crop for the respective numbered garment, in image order. "
            f"Expected items in order: {json.dumps(expected, ensure_ascii=False)}. "
            "The crop must clearly show the named piece as its main subject. Reject images dominated by "
            "bare skin, background, a different garment, or showing only a neckline, hem, buttons or other detail. "
            "For shorts the garment must be visible with its waist and hem distinguishable. For shoes, both "
            "shoes need not be fully visible, but their shape and sole must be identifiable. "
            "Judge only pixels in that crop. Mark uncertain cases false. Return JSON with an accepted array "
            "of one boolean per image, in the same order.",
            *(_image_data_uri(crop) for crop in crops[1:]),
        )
        result = json.loads(self._strip_json_fence(content))
        marks = result.get("accepted") if isinstance(result, dict) else None
        if not isinstance(marks, list) or len(marks) != len(crops) or not all(isinstance(mark, bool) for mark in marks):
            raise ValueError("Invalid garment crop review response")
        return cast(list[bool], marks)

    async def analyze_style(
        self, image_url: str, locale: str = DEFAULT_LOCALE, *, source_key: str | None = None
    ) -> StyleAnalysis:
        # `locale` is accepted for interface parity but ignored: style tags must
        # be stable English so the recommendation template matcher can key on
        # them (same reason category/color stay English in analyze_image).
        image_payload_url = await self._to_data_uri(image_url, source_key)
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

    async def matches_product_photo(
        self, source: bytes, generated: bytes, *, category: str, title: str, color: str
    ) -> bool:
        expected = json.dumps({"category": category, "title": title, "color": color}, ensure_ascii=False)
        content = await self._request_content(
            _image_data_uri(source),
            "Check a generated clothing product photo against the real reference. The first image "
            "is the reference and the second is the generated product. "
            f"Expected item metadata: {expected}. "
            "Accept only if the named item is clearly visible in the reference and the product shows "
            "that same item, preserving its visible color, geometry, material, construction and markings. "
            "Reject a different garment, extra clothing, or changed visible details. "
            "Allow plain, consistent completion of parts genuinely hidden by another garment or the frame. "
            "Allow reasonable changes in folds and lighting from wearing versus a product display. "
            "Compare exact visible lettering or graphic shape, orientation, position and scale of marks. "
            "Inspect waistbands. Reject guessed lettering where a mark is visible but too blurry to read. "
            "Check shoe toe panels, perforations, stitching "
            "and sole shape. Score corresponding visible areas. A generic match in type and color is insufficient. "
            "Return only JSON with a boolean field accepted.",
            _image_data_uri(generated),
        )
        result = json.loads(self._strip_json_fence(content))
        return isinstance(result, dict) and result.get("accepted") is True

    async def matches_outfit_photo(
        self, sources: list[bytes], generated: bytes, *, garments: list[str]
    ) -> bool:
        content = await self._request_content(
            _image_data_uri(generated),
            "Verify the outfit flat lay in image 1 using only the references that follow it. "
            "The names help identify the target in a reference and do not prove any feature is visible. "
            f"The reference garments in order are {json.dumps(garments, ensure_ascii=False)}. "
            "Check each target garment separately. Match the visible color, neckline or collar, sleeves, "
            "hem, leg width, fabric, pockets, seams, fasteners, and the side and position of marks. "
            "Reject mirrored or moved visible details, changed visible construction, or extra pieces. "
            "Allow a new angle and completion of areas genuinely hidden in the references. "
            "Check for material changes to any area visible in both images. Specifically compare shoe "
            "toe shape, visible seams and surface markings. A smooth visible toe panel cannot gain holes "
            "or a logo. The previously hidden heel or opening may now be visible. Detached shoe fragments fail. "
            "Decide whether each item is present independently from whether its details match. "
            "Mark uncertainty as false. "
            'Return JSON with "composition_matches" (boolean for every piece present and no extras) and '
            '"garment_matches" (array of one boolean per reference, in reference order, true only if '
            "the photographed visible features match).",
            *(_image_data_uri(source) for source in sources),
        )
        result = json.loads(self._strip_json_fence(content))
        if not isinstance(result, dict):
            raise ValueError("Invalid outfit quality review response")
        marks = result.get("garment_matches")
        if (
            not isinstance(marks, list) or len(marks) != len(garments)
            or not all(isinstance(mark, bool) for mark in marks)
            or not isinstance(result.get("composition_matches"), bool)
        ):
            raise ValueError("Invalid outfit quality review response")
        return result["composition_matches"] is True and all(marks)

    async def matches_outfit_footwear(self, source: bytes, generated: bytes) -> bool:
        content = await self._request_content(
            _image_data_uri(source),
            "Compare the actual shoe photo in image 1 with the shoes at the bottom of the outfit in image 2. "
            "Ignore all other garments except where they occlude the shoes. Compare the two photos visually. "
            "Allow reconstruction of the photographed shoe's hidden areas, including its heel, collar and "
            "opening. Compare the same regions in both images, accounting for viewpoint. Check toe shape, "
            "panel contours, edge seams, visible laces and sole. Compare whether the visible toe seam "
            "is dark or light against the upper, and whether it traces the same path around the toe. "
            "Reject changes to those features, including "
            "perforations, logos or decoration on a surface visible without them in image 1. "
            "A complete shoe may look different in the previously hidden areas; score only the visible evidence. "
            "Detached shoe fragments fail. "
            'Return JSON with boolean fields "visible_design_matches" and "no_changes_on_visible_regions".',
            _image_data_uri(generated),
        )
        result = json.loads(self._strip_json_fence(content))
        if not isinstance(result, dict):
            raise ValueError("Invalid footwear quality review response")
        visible_design = result.get("visible_design_matches")
        details_preserved = result.get("no_changes_on_visible_regions")
        if not isinstance(visible_design, bool) or not isinstance(details_preserved, bool):
            raise ValueError("Invalid footwear quality review response")
        return visible_design and details_preserved

    async def _request_content(
        self, image_url: str, prompt: str, *reference_urls: str, source_key: str | None = None
    ) -> str:
        # Inline private wardrobe images instead of exposing signed URLs to the
        # third-party vision provider.
        image_payload_url = await self._to_data_uri(image_url, source_key)
        payload = {
            "model": self.settings.QWEN_VISION_MODEL,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": image_payload_url}},
                        *(
                            {"type": "image_url", "image_url": {"url": url}}
                            for url in reference_urls
                        ),
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

    async def _to_data_uri(self, image_url: str, source_key: str | None = None) -> str:
        # Already inlined (e.g. a caller passed a data URI) — pass through.
        if image_url.startswith("data:"):
            return image_url
        # Our own uploads are read from storage by key: the public URL is for
        # devices and may not resolve from the worker (in the local stack it is
        # localhost:9000). External import links are fetched behind the SSRF guard.
        return image_data_uri(await load_source_bytes(image_url, source_key))

    @staticmethod
    def _strip_json_fence(content: str) -> str:
        text = content.strip()
        if text.startswith("```"):
            text = text.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
        return text

    def _parse_json_content(self, content: str) -> dict[str, Any]:
        raw = json.loads(self._strip_json_fence(content))
        if isinstance(raw, dict) and raw.get("no_garment") is True:
            raise NoGarmentsDetected
        return self._validate_vision_data(raw)

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


def _image_data_uri(data: bytes) -> str:
    return f"data:{_image_mime(data)};base64,{base64.b64encode(data).decode('ascii')}"


def _image_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG"):
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return "image/jpeg"


def garment_box(raw: Any, *, scale: int = 1) -> tuple[float, float, float, float] | None:
    """A model-reported garment box as clamped fractions, or None if unusable.

    Rejects malformed, inverted or tiny boxes (under 5% of a side) — a bad box
    must leave the candidate on the full photo, never crop to the wrong place.
    """
    if not isinstance(raw, list) or len(raw) != 4:
        return None
    if not all(isinstance(v, int | float) and not isinstance(v, bool) and math.isfinite(v) for v in raw):
        return None
    if scale == 1000 and not all(isinstance(v, int) and 0 <= v <= scale for v in raw):
        return None
    x1, y1, x2, y2 = (max(0.0, min(1.0, v / scale)) for v in raw)
    if x2 - x1 < _MIN_BOX_SIDE or y2 - y1 < _MIN_BOX_SIDE:
        return None
    return (x1, y1, x2, y2)
