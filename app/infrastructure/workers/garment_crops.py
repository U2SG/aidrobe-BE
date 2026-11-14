"""Per-garment crops for multi-garment import photos.

Recognition boxes each garment in an outfit photo; this cuts those regions out
of the user's original (pixels unchanged, only framed), stores each crop, and
points the candidate at it so every confirmed item gets its own picture instead
of the whole outfit photo. The full photo stays the item's `original`.

Fail-soft throughout: a missing box, an unreadable source or a storage error
leaves that candidate on the full photo — cropping must never fail an import.
"""

from __future__ import annotations

import asyncio
import io
import uuid
from collections.abc import Sequence
from typing import Any

import structlog
from PIL import Image, ImageOps

from app.application.ports.vision import VisionPort, VisionResult
from app.infrastructure.storage.object_key import garment_crop_object_key
from app.infrastructure.storage.source_image import load_source_bytes

logger = structlog.get_logger(__name__)

#: Breathing room around the model's box, as a fraction of the box size, so a
#: slightly tight box does not clip a sleeve or a hem.
CROP_PADDING = 0.06
CROP_JPEG_QUALITY = 90

Box = tuple[float, float, float, float]


def crop_regions(image_bytes: bytes, boxes: Sequence[Box | None]) -> list[bytes | None]:
    """JPEG crops of `boxes` (fractions of width/height); None where no box."""
    with Image.open(io.BytesIO(image_bytes)) as opened:
        image = ImageOps.exif_transpose(opened).convert("RGB")
    width, height = image.size
    crops: list[bytes | None] = []
    for box in boxes:
        if box is None:
            crops.append(None)
            continue
        x1, y1, x2, y2 = box
        pad_x = (x2 - x1) * CROP_PADDING
        pad_y = (y2 - y1) * CROP_PADDING
        left = max(0, int((x1 - pad_x) * width))
        top = max(0, int((y1 - pad_y) * height))
        right = min(width, int(round((x2 + pad_x) * width)))
        bottom = min(height, int(round((y2 + pad_y) * height)))
        if right - left < 2 or bottom - top < 2:
            crops.append(None)
            continue
        buffer = io.BytesIO()
        image.crop((left, top, right, bottom)).save(buffer, format="JPEG", quality=CROP_JPEG_QUALITY)
        crops.append(buffer.getvalue())
    return crops


async def attach_garment_crops(
    candidates: list[dict[str, Any]],
    results: Sequence[VisionResult],
    *,
    image_url: str,
    user_id: uuid.UUID,
    task_id: uuid.UUID,
    vision: VisionPort,
) -> None:
    """Add `bbox`, `crop_image_url` and `crop_object_key` to the candidates
    whose garment was boxed. Candidates and results are parallel lists."""
    boxes = [result.bbox for result in results]
    for candidate, box in zip(candidates, boxes, strict=True):
        if box is not None:
            candidate["bbox"] = list(box)
    if not any(boxes):
        return
    try:
        from app.bootstrap.container import object_storage

        source = await load_source_bytes(image_url, None)
        crops = await asyncio.to_thread(crop_regions, source, boxes)
        framed = [
            (candidate, crop, result)
            for candidate, crop, result in zip(candidates, crops, results, strict=True)
            if crop is not None
        ]
        if not framed:
            return
        accepted = await vision.review_garment_crops(
            [crop for _, crop, _ in framed], [result for _, _, result in framed]
        )
        storage = object_storage()
        for (candidate, crop, _), approved in zip(framed, accepted, strict=True):
            if not approved:
                continue
            key = garment_crop_object_key(user_id, task_id, str(candidate["temp_item_id"]))
            candidate["crop_image_url"] = await storage.put_object(key, crop, "image/jpeg")
            candidate["crop_object_key"] = key
    except Exception as exc:  # noqa: BLE001 — cropping is best-effort by design
        logger.warning("garment_crops.failed", task_id=str(task_id), error=str(exc))
