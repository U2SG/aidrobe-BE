"""Outfit board image generation — second vertical slice on `job_runtime`.

Producers create an `OutfitVisualAsset` row (`generation_status='queued'`) and
call `enqueue_outfit_board` in the same transaction. The async-jobs row holds
retry/lease/idempotency state; the asset row is the user-visible read model.

`drain_outfit_board_jobs` claims one job at a time, renders the board, writes
`image_url` back to the asset, and finalizes via `mark_succeeded` /
`mark_failed`. A cron tick recovers expired leases so a hard-killed worker
can't pin a board for hours.

Idempotency:
- with `outfit_id` known: `outfit_board:{outfit_id}:{layout_version}`
- otherwise (ad-hoc try-on / preview): `outfit_board:asset:{asset_id}`

Enqueueing the same `(outfit_id, layout_version)` twice is a no-op — we hand
back the existing job and the existing asset gets reused on the read side.
"""

from __future__ import annotations

import io
import uuid

import structlog
from PIL import Image, ImageDraw
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports.storage import ObjectStoragePort
from app.config.settings import get_settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.storage.object_key import names_internal_storage, trusted_item_image_key
from app.infrastructure.storage.source_image import download_external_source_image
from app.infrastructure.vision.openai_image_cutout import OpenAIImageCutout
from app.infrastructure.vision.qwen_vision import QwenVision
from app.infrastructure.workers._visual_asset_runtime import (
    VisualAssetSnapshot,
    drain_visual_jobs,
    reconcile_dead_visual_assets,
)
from app.infrastructure.workers.job_runtime import enqueue

logger = structlog.get_logger(__name__)

JOB_TYPE = "outfit_board"
# Bounded so a single worker tick doesn't starve other arq jobs. Boards are
# heavier than embeddings (image download + composite + S3 put) so this is
# tighter than the embedding budget.
MAX_DRAIN_PER_TICK = 5

CANVAS_W, CANVAS_H = 800, 1000
PLACEHOLDER_COLOR = (220, 220, 220)
PLACEHOLDER_TEXT_COLOR = (150, 150, 150)

LAYOUT: dict[str, tuple[int, int, int, int]] = {
    "outerwear": (250, 20, 300, 280),
    "top": (40, 320, 280, 280),
    "bag": (480, 320, 240, 240),
    "bottom": (200, 610, 320, 280),
    "dress": (200, 320, 320, 440),
    "shoes": (280, 880, 240, 100),
}
DEFAULT_BOX = (200, 400, 320, 320)


def _idempotency_key(*, outfit_id: uuid.UUID | None, asset_id: uuid.UUID, layout_version: str) -> str:
    if outfit_id is not None:
        return f"outfit_board:{outfit_id}:{layout_version}"
    return f"outfit_board:asset:{asset_id}"


async def enqueue_outfit_board(
    session: AsyncSession,
    *,
    asset_id: uuid.UUID,
    layout_version: str,
    outfit_id: uuid.UUID | None = None,
    user_id: uuid.UUID | None = None,
    correlation_id: str | None = None,
) -> tuple[AsyncJob, bool]:
    """Idempotent enqueue. Caller owns commit and is responsible for having
    already inserted the matching `OutfitVisualAsset` row."""
    return await enqueue(
        session,
        job_type=JOB_TYPE,
        idempotency_key=_idempotency_key(outfit_id=outfit_id, asset_id=asset_id, layout_version=layout_version),
        payload={
            "asset_id": str(asset_id),
            "layout_version": layout_version,
            "outfit_id": str(outfit_id) if outfit_id else None,
        },
        user_id=user_id,
        target_id=asset_id,
        correlation_id=correlation_id,
    )


def _image_from_bytes(data: bytes) -> Image.Image | None:
    try:
        return Image.open(io.BytesIO(data)).convert("RGBA")
    except Exception:
        return None


async def _download_image(url: str) -> Image.Image | None:
    try:
        data, _ = await download_external_source_image(url, get_settings())
        return _image_from_bytes(data)
    except Exception:
        return None


async def _load_snapshot_image(
    storage: ObjectStoragePort, *, object_key: str | None, fallback_url: str | None, user_id: uuid.UUID
) -> Image.Image | None:
    """Prefer durable object storage while keeping URL-only legacy snapshots readable."""
    if object_key:
        try:
            image = _image_from_bytes(await storage.get_object(object_key))
            if image is not None:
                return image
            if fallback_url is None:
                raise ValueError(f"invalid image bytes in object storage: {object_key}")
        except Exception:
            if fallback_url is None:
                raise
    if fallback_url:
        # Most item images are stored as our own public file URL with no key.
        # Read those from storage: the public host is for devices and may not
        # resolve from the worker (and the SSRF guard rightly blocks private
        # hosts), which left boards full of placeholders.
        settings = get_settings()
        own_key = trusted_item_image_key(fallback_url, user_id, settings)
        if own_key:
            image = _image_from_bytes(await storage.get_object(own_key))
            if image is None:
                raise ValueError(f"invalid image bytes in object storage: {own_key}")
            return image
        if not names_internal_storage(fallback_url, settings):
            return await _download_image(fallback_url)
    return None


def centered_boxes(boxes: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
    """Shift the used slots as a group to the middle of the canvas.

    The slots are fixed per role (outerwear on top, bag on the right), so an
    outfit without those left a blank band at the top and an empty right half.
    Sizes and relative positions are kept; only the group moves.
    """
    if not boxes:
        return boxes
    left = min(x for x, _, _, _ in boxes)
    top = min(y for _, y, _, _ in boxes)
    right = max(x + w for x, _, w, _ in boxes)
    bottom = max(y + h for _, y, _, h in boxes)
    dx = (CANVAS_W - (right - left)) // 2 - left
    dy = (CANVAS_H - (bottom - top)) // 2 - top
    return [(x + dx, y + dy, w, h) for x, y, w, h in boxes]


def _paste_item(canvas: Image.Image, item_img: Image.Image, box: tuple[int, int, int, int]) -> None:
    x, y, w, h = box
    item_img = item_img.copy()
    item_img.thumbnail((w, h), Image.Resampling.LANCZOS)
    px = x + (w - item_img.width) // 2
    py = y + (h - item_img.height) // 2
    if item_img.mode == "RGBA":
        canvas.paste(item_img, (px, py), item_img)
    else:
        canvas.paste(item_img, (px, py))


def _draw_placeholder(canvas: Image.Image, box: tuple[int, int, int, int], label: str) -> None:
    draw = ImageDraw.Draw(canvas)
    x, y, w, h = box
    draw.rectangle([x, y, x + w, y + h], fill=PLACEHOLDER_COLOR)
    text = label[:12]
    bbox = draw.textbbox((0, 0), text)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(
        (x + (w - tw) // 2, y + (h - th) // 2),
        text,
        fill=PLACEHOLDER_TEXT_COLOR,
    )


def _jpeg_reference(image: Image.Image) -> bytes:
    scale = min(1024 / max(image.size), max(1.0, 512 / max(image.size)))
    if scale != 1:
        size = (max(1, round(image.width * scale)), max(1, round(image.height * scale)))
        image = image.resize(size, Image.Resampling.LANCZOS)
    frame = io.BytesIO()
    image.convert("RGB").save(frame, format="JPEG", quality=90)
    return frame.getvalue()


async def _render_and_upload(snapshot: VisualAssetSnapshot) -> tuple[str, str] | None:
    """Render the board and upload it if its references support the result."""
    from app.bootstrap.container import object_storage

    items = snapshot.input_snapshot.get("item_list", [])
    canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), (255, 255, 255, 255))
    storage = object_storage()
    roles = [(item.get("role") or item.get("category") or "top").lower() for item in items]
    boxes = centered_boxes([LAYOUT.get(role, DEFAULT_BOX) for role in roles])
    generate = snapshot.input_snapshot.get("generate") is True
    settings = get_settings()
    if generate and (not settings.ENABLE_ITEM_CUTOUT or settings.IMAGE_CUTOUT_PROVIDER != "openai"):
        raise ValueError("Outfit image generation is disabled")
    generation_images: list[bytes] = []
    review_images: list[bytes] = []
    garments: list[str] = []
    footwear_reference: bytes | None = None

    for item, role, box in zip(items, roles, boxes, strict=True):
        img = await _load_snapshot_image(
            storage,
            object_key=item.get("cutout_object_key"),
            fallback_url=item.get("cutout_image_url"),
            user_id=snapshot.user_id,
        )
        if img is None:
            img = await _load_snapshot_image(
                storage,
                object_key=item.get("thumbnail_object_key"),
                fallback_url=item.get("thumbnail_url"),
                user_id=snapshot.user_id,
            )

        if generate:
            if img is None:
                return None
            frame = _jpeg_reference(img)
            review_frame = frame
            review_key = item.get("review_object_key")
            review_url = item.get("review_image_url")
            if review_key or review_url:
                reference = await _load_snapshot_image(
                    storage, object_key=review_key, fallback_url=review_url, user_id=snapshot.user_id,
                )
                if reference is None:
                    return None
                review_frame = _jpeg_reference(reference)
            shoes = role == "shoes" or item.get("category") == "shoes"
            generation_images.append(review_frame if shoes else frame)
            review_images.append(review_frame)
            if shoes:
                # Recommendations have one footwear slot. A board with
                # multiple pairs cannot reliably assign them to references.
                if footwear_reference is not None:
                    return None
                footwear_reference = review_frame
            title = item.get("title") or role
            color = item.get("color")
            garments.append(f"{role}: {title} ({color})" if color else f"{role}: {title}")
        elif img is not None:
            _paste_item(canvas, img, box)
        else:
            _draw_placeholder(canvas, box, role)

    if generate:
        if not generation_images:
            return None
        provider = OpenAIImageCutout(settings)
        reviewer = QwenVision(settings)
        attempts = (False, True) if footwear_reference is not None else (False,)
        for emphasize_visible_details in attempts:
            generated = await provider.render_outfit(
                generation_images, garments, emphasize_shoe_details=emphasize_visible_details
            )
            if not await reviewer.matches_outfit_photo(review_images, generated, garments=garments):
                continue
            if footwear_reference is not None and not await reviewer.matches_outfit_footwear(
                footwear_reference, generated
            ):
                continue
            key = f"users/{snapshot.user_id}/outfit_boards/{snapshot.id}-{snapshot.attempt}.png"
            return await storage.put_object(key, generated, "image/png"), key
        return None

    rgb = canvas.convert("RGB")
    buf = io.BytesIO()
    rgb.save(buf, format="JPEG", quality=85)
    buf.seek(0)

    key = f"{snapshot.user_id}/outfit_boards/{snapshot.id}.jpg"
    url = await storage.put_object(key, buf.read(), "image/jpeg")
    return url, key


async def reconcile_dead_outfit_board_assets(ctx: object) -> int:
    return await reconcile_dead_visual_assets(JOB_TYPE, "outfit_board")


async def drain_outfit_board_jobs(ctx: object) -> int:
    return await drain_visual_jobs(
        JOB_TYPE,
        _render_and_upload,
        MAX_DRAIN_PER_TICK,
        "outfit_board",
        asset_not_found_event="outfit_visual_asset_not_found",
    )
