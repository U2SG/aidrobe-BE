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

import httpx
import structlog
from PIL import Image, ImageDraw
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.persistence.models.async_job import AsyncJob
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
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return _image_from_bytes(resp.content)
    except Exception:
        return None


async def _load_snapshot_image(storage, *, object_key: str | None, fallback_url: str | None) -> Image.Image | None:
    """Prefer durable object storage while keeping URL-only legacy snapshots readable."""
    if object_key:
        try:
            image = _image_from_bytes(await storage.get_object(object_key))
            if image is not None:
                return image
        except Exception:
            pass
    if fallback_url:
        return await _download_image(fallback_url)
    return None


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


async def _render_and_upload(snapshot: VisualAssetSnapshot) -> tuple[str, str]:
    """Render the composite board and upload to object storage."""
    from app.bootstrap.container import object_storage

    items = snapshot.input_snapshot.get("item_list", [])
    canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), (255, 255, 255, 255))
    storage = object_storage()

    for item in items:
        role = (item.get("role") or item.get("category") or "top").lower()
        box = LAYOUT.get(role, DEFAULT_BOX)

        img = await _load_snapshot_image(
            storage,
            object_key=item.get("cutout_object_key"),
            fallback_url=item.get("cutout_image_url"),
        )
        if img is None:
            img = await _load_snapshot_image(
                storage,
                object_key=item.get("thumbnail_object_key"),
                fallback_url=item.get("thumbnail_url"),
            )

        if img is not None:
            _paste_item(canvas, img, box)
        else:
            _draw_placeholder(canvas, box, role)

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
