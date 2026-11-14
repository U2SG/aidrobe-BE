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
from app.infrastructure.vision.qwen_vision import QwenVision

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


def _prompt(category: str, title: str | None = None, color: str | None = None) -> str:
    noun = _NOUN.get(category, _DEFAULT_NOUN)
    target = f" Target garment: {title}." if title else ""
    if color:
        target += f" Target color: {color}."
    return (
        f"Isolate ONLY the {noun} from this photo and present it as a clean flat-lay "
        "e-commerce product photo on a pure solid white (#ffffff) background."
        f"{target} Treat any other garments, including overlaid layers, as surrounding objects. Remove any "
        "person, hands, phone, mirror, hangers and the room/background. Preserve the exact "
        "color, shape, material, sleeves/straps and all details — do not restyle, recolor, "
        "or invent details."
    )


class OpenAIImageCutout:
    def __init__(self, settings: Settings) -> None:
        if not settings.OPENAI_API_KEY:
            raise ValueError("OPENAI_API_KEY is required for the openai image cutout provider")
        self.settings = settings

    async def extract(
        self, image_bytes: bytes, category: str, *, title: str | None = None, color: str | None = None
    ) -> bytes | None:
        if not title or not color or not self.settings.QWEN_API_KEY:
            return None
        generated = await self._edit([image_bytes], _prompt(category, title, color))
        reviewer = QwenVision(self.settings)
        if await reviewer.matches_product_photo(image_bytes, generated, category=category, title=title, color=color):
            return generated
        return None

    async def render_outfit(self, images: list[bytes], garments: list[str]) -> bytes:
        pieces = "; ".join(f"image {index}: {garment}" for index, garment in enumerate(garments, 1))
        return await self._edit(
            images,
            "Create one cohesive fashion flat lay on a clean light neutral background. "
            f"The numbered references show these garments: {pieces}. Show every listed garment and no extras. "
            "Match the actual color, fabric, cut, seams, fasteners and markings visible in each reference. "
            "Keep any areas hidden by fabric, cropping or framing hidden in the finished outfit. "
            "For partly covered shoes, trace the photographed toe outlines and seams, and pull the trouser "
            "hems far enough over each shoe to keep all unverified details covered. Preserve asymmetry "
            "and partial framing. Do not reconstruct hidden shoe openings, laces, heels, logos, perforations, "
            "sides or soles, and do not show detached shoe fragments. Keep the shoes naturally aligned under "
            "the trouser hems. Keep the other garments recognizable as a full coordinated look. "
            "Remove people, phones, hangers and surroundings. Use no text or labels.",
        )

    async def _edit(self, images: list[bytes], prompt: str) -> bytes:
        s = self.settings
        field = "image" if len(images) == 1 else "image[]"
        files = [(field, (f"source-{index}.jpg", image, "image/jpeg")) for index, image in enumerate(images)]
        data: dict[str, Any] = {
            "model": s.OPENAI_IMAGE_MODEL,
            "prompt": prompt,
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
