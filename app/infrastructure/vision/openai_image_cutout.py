"""gpt-image-2 product-shot cutout via the OpenAI images/edits endpoint.

Generative extraction: the model redraws the item onto a white background,
removing the person/room. Validated on real wardrobe photos in a bake-off —
`quality=low` is the cost/quality sweet spot (~$0.024/img, ~35s) and preserves
fine detail (studs, necklines, lace). It does NOT support transparent
background, so the prompt asks for solid white.

Async-slow (35-200s by quality), so this only ever runs inside the `cutout`
async job, never on a request path.
"""

from __future__ import annotations

import base64
from typing import Any

import httpx

from app.config.settings import Settings

_EDITS_URL = "https://api.openai.com/v1/images/edits"

# category (from VisionResult) → the noun the prompt isolates.
_NOUN: dict[str, str] = {
    "top": "top / shirt the person is wearing",
    "bottom": "bottom garment (pants / skirt / shorts)",
    "outerwear": "jacket / coat",
    "dress": "dress",
    "shoes": "pair of shoes",
    "accessory": "accessory item",
    "other": "clothing item",
}
_DEFAULT_NOUN = "clothing item"


def _prompt(category: str) -> str:
    noun = _NOUN.get(category, _DEFAULT_NOUN)
    return (
        f"Isolate ONLY the {noun} from this photo and present it as a clean flat-lay "
        "e-commerce product photo on a pure solid white (#ffffff) background. Remove any "
        "person, hands, phone, mirror, hangers and the room/background. Preserve the exact "
        "color, shape, material, sleeves/straps and all details — do not restyle, recolor, "
        "or invent details."
    )


class OpenAIImageCutout:
    def __init__(self, settings: Settings) -> None:
        if not settings.OPENAI_API_KEY:
            raise ValueError("OPENAI_API_KEY is required for the openai image cutout provider")
        self.settings = settings

    async def extract(self, image_bytes: bytes, category: str) -> bytes:
        s = self.settings
        files = {"image": ("source.jpg", image_bytes, "image/jpeg")}
        data: dict[str, Any] = {
            "model": s.OPENAI_IMAGE_MODEL,
            "prompt": _prompt(category),
            "quality": s.IMAGE_CUTOUT_QUALITY,
            "size": "auto",
            "n": "1",
        }
        async with httpx.AsyncClient(timeout=s.IMAGE_CUTOUT_TIMEOUT_SECONDS) as client:
            resp = await client.post(
                _EDITS_URL,
                headers={"Authorization": f"Bearer {s.OPENAI_API_KEY}"},
                files=files,
                data=data,
            )
            resp.raise_for_status()
            payload = resp.json()
        b64 = payload["data"][0].get("b64_json")
        if not b64:
            raise RuntimeError("gpt-image edit returned no image data")
        return base64.b64decode(b64)
