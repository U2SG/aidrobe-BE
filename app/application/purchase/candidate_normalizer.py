"""V3-004 — normalize a purchase candidate into the frozen `parsed_item` shape.

Pure functions, no DB and no IO, so every source type collapses onto one
normalized contract (`docs/v3-p0-contract.md` §5.2):

```jsonc
{"category", "colors": [], "silhouette", "material_hints": [], "price", "scene_tags": []}
```

**Where untrusted text stops.** A product page's title and meta tags are
attacker-influenced text. Two bounds apply, in this order:

1. `sanitize_llm_text` (Phase E4 / BE#46 bound-at-write) strips control
   characters and newlines — the vehicle for multi-line prompt injection —
   collapses whitespace and hard-caps the length. Everything below reads only
   the sanitized text, never the raw text.
2. Extraction is **whitelist- and regex-bounded**: attributes can only take a
   value from the vocabularies in this module, and `price` can only be a
   currency/amount match. So no span of page text is ever copied verbatim into
   a persisted field.

That is why V3-007 can feed `parsed_item` to an LLM safely: by construction it
holds our own vocabulary plus a numeric price, not page prose. The raw page
text is never persisted — the frozen shape has no field for it, and keeping it
would be a privacy and injection liability for no decision value.

Vocabularies are bilingual because the link sources this reuses are CN
shopping sites (Taobao / Tmall / XHS / Douyin adapters) alongside global ones.
Values are always emitted as the English canonical term so downstream
similarity (V3-005) and decision reasons (V3-007) key off one vocabulary.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from app.infrastructure.llm.sanitizer import sanitize_llm_text

# Canonical English term -> the surface forms that imply it. Matching is
# substring-based on the lowercased sanitized text, so CJK needs no tokenizer.
MATERIAL_HINTS: dict[str, tuple[str, ...]] = {
    "cotton": ("cotton", "棉"),
    "wool": ("wool", "羊毛", "merino"),
    "cashmere": ("cashmere", "羊绒"),
    "linen": ("linen", "亚麻", "麻质"),
    "silk": ("silk", "真丝", "丝绸"),
    "denim": ("denim", "牛仔"),
    "leather": ("leather", "皮革", "真皮"),
    "down": ("down fill", "羽绒"),
    "knit": ("knit", "针织"),
    "polyester": ("polyester", "涤纶"),
    "viscose": ("viscose", "rayon", "粘纤", "粘胶"),
    "fleece": ("fleece", "抓绒"),
}

SCENE_TAGS: dict[str, tuple[str, ...]] = {
    "commute": ("commute", "通勤"),
    "office": ("office", "workwear", "职场", "办公"),
    "formal": ("formal", "正式", "礼服"),
    "interview": ("interview", "面试"),
    "wedding": ("wedding", "婚礼", "伴娘"),
    "party": ("party", "派对", "聚会"),
    "date": ("date night", "约会"),
    "travel": ("travel", "旅行", "度假"),
    "sport": ("sport", "athletic", "运动", "健身"),
    "outdoor": ("outdoor", "hiking", "户外", "登山"),
    "casual": ("casual", "everyday", "休闲", "日常"),
}

SILHOUETTES: dict[str, tuple[str, ...]] = {
    "oversized": ("oversized", "oversize", "廓形", "宽松"),
    "slim": ("slim fit", "slim-fit", "修身"),
    "fitted": ("fitted", "紧身"),
    "straight": ("straight leg", "straight-leg", "直筒"),
    "wide_leg": ("wide leg", "wide-leg", "阔腿"),
    "cropped": ("cropped", "短款"),
    "longline": ("longline", "长款"),
    "a_line": ("a-line", "a字"),
}

# `price` stays a display string (contract §5.2) — a shopping page shows
# "¥299.00" / "$49.99", and coercing to a number would invent a currency the
# page never stated. Bounded to a currency-or-unit match so no prose leaks in.
_PRICE_RE = re.compile(
    r"(?:(?P<symbol>[¥￥$€£]|RMB|CNY|USD)\s?(?P<amount>\d[\d,]{0,11}(?:\.\d{1,2})?)"
    r"|(?P<amount2>\d[\d,]{0,11}(?:\.\d{1,2})?)\s?(?P<unit>元|RMB|CNY))",
    re.IGNORECASE,
)

# Cap the emitted lists so a keyword-stuffed page cannot inflate a snapshot.
_MAX_HINTS = 5


@dataclass(frozen=True)
class ParsedItem:
    """The frozen minimal shape. `None` means "we did not determine this" —
    deliberately not `[]`, so a client can tell "unknown" from "none found"."""

    category: str | None = None
    colors: list[str] | None = None
    silhouette: str | None = None
    material_hints: list[str] | None = None
    price: str | None = None
    scene_tags: list[str] | None = None
    # Coarse garment type ("tee", "turtleneck", "jeans"…; see garment_type.py).
    # Additive to the frozen §5.2 shape; None when recognition gave no name.
    garment_type: str | None = None

    def as_payload(self) -> dict[str, object]:
        """JSONB/response payload; every contract key is always present."""
        return {
            "category": self.category,
            "colors": self.colors,
            "silhouette": self.silhouette,
            "material_hints": self.material_hints,
            "price": self.price,
            "scene_tags": self.scene_tags,
            "garment_type": self.garment_type,
        }

    @property
    def is_usable(self) -> bool:
        """Enough to decide on. `category` is the minimum: V3-005 similarity and
        V3-007 redundancy both key off it, and nothing else substitutes."""
        return bool(self.category)


def bound_untrusted_text(raw: str | None) -> str:
    """The single entry point for page/OCR text. See the module docstring."""
    if not raw:
        return ""
    return sanitize_llm_text(raw)


def _matches(text: str, vocabulary: dict[str, tuple[str, ...]]) -> list[str]:
    return [
        canonical
        for canonical, surface_forms in vocabulary.items()
        if any(form in text for form in surface_forms)
    ]


def _first_match(text: str, vocabulary: dict[str, tuple[str, ...]]) -> str | None:
    matches = _matches(text, vocabulary)
    return matches[0] if matches else None


def extract_price(text: str) -> str | None:
    """First currency-tagged amount in the bounded text, normalized for display."""
    match = _PRICE_RE.search(text)
    if match is None:
        return None
    if match.group("amount"):
        symbol = (match.group("symbol") or "").upper().replace("￥", "¥")
        return f"{symbol}{match.group('amount')}"
    unit = (match.group("unit") or "").upper()
    return f"{match.group('amount2')}{'元' if unit == '元' else f' {unit}'}"


def normalize_candidate(
    *,
    category: str | None = None,
    color: str | None = None,
    price: str | None = None,
    raw_text: str | None = None,
    garment_type: str | None = None,
    material: str | None = None,
) -> ParsedItem:
    """Fold a vision/OCR reading plus (untrusted) page text into `parsed_item`.

    `category` / `color` come from a provider (`VisionResult` / `OcrResult`) and
    are already our own enum-ish vocabulary. `price` is a provider-supplied
    value (OCR reads it off the receipt) and wins over one scraped from text.
    `raw_text` is untrusted and is bounded before it is read.
    """
    text = bound_untrusted_text(raw_text).lower()
    materials = _matches(text, MATERIAL_HINTS)[:_MAX_HINTS]
    # A recognised material (provider vocabulary) leads the page-text hints.
    provider_material = (material or "").strip().lower()
    if provider_material in MATERIAL_HINTS and provider_material not in materials:
        materials = [provider_material, *materials][:_MAX_HINTS]
    scenes = _matches(text, SCENE_TAGS)[:_MAX_HINTS]
    return ParsedItem(
        category=category or None,
        colors=[color] if color else None,
        silhouette=_first_match(text, SILHOUETTES),
        material_hints=materials or None,
        price=price or extract_price(text),
        scene_tags=scenes or None,
        garment_type=garment_type,
    )
