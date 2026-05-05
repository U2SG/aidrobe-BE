"""Outfit board image generation worker."""

import io
import uuid
from dataclasses import dataclass

import httpx
import structlog
from PIL import Image, ImageDraw

from app.infrastructure.persistence.session import AsyncSessionLocal

logger = structlog.get_logger(__name__)

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


@dataclass
class _AssetSnapshot:
    """Plain data extracted from DB before session closes."""
    id: uuid.UUID
    user_id: uuid.UUID
    input_snapshot: dict


async def _download_image(url: str) -> Image.Image | None:
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(url)
            resp.raise_for_status()
            return Image.open(io.BytesIO(resp.content)).convert("RGBA")
    except Exception:
        return None


def _paste_item(canvas: Image.Image, item_img: Image.Image, box: tuple[int, int, int, int]) -> None:
    x, y, w, h = box
    item_img = item_img.copy()
    item_img.thumbnail((w, h), Image.LANCZOS)
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
    # anchor="mm" requires TrueType font; use manual centering with default font
    text = label[:12]
    bbox = draw.textbbox((0, 0), text)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(
        (x + (w - tw) // 2, y + (h - th) // 2),
        text,
        fill=PLACEHOLDER_TEXT_COLOR,
    )


async def generate_outfit_board(ctx: object, asset_id: str) -> None:
    from app.infrastructure.persistence.repositories.outfit_visual_repo import (
        SqlAlchemyOutfitVisualRepository,
    )

    # Load and snapshot all needed data before closing the session (fixes DetachedInstanceError)
    async with AsyncSessionLocal() as session:
        repo = SqlAlchemyOutfitVisualRepository(session)
        asset = await repo.get_by_id(uuid.UUID(asset_id))
        if asset is None:
            logger.warning("outfit_visual_asset_not_found", asset_id=asset_id)
            return
        snapshot = _AssetSnapshot(
            id=asset.id,
            user_id=asset.user_id,
            input_snapshot=dict(asset.input_snapshot or {}),
        )
        await repo.update_status(asset.id, "processing")
        await session.commit()

    try:
        items = snapshot.input_snapshot.get("item_list", [])
        canvas = Image.new("RGBA", (CANVAS_W, CANVAS_H), (255, 255, 255, 255))

        for item in items:
            role = (item.get("role") or item.get("category") or "top").lower()
            box = LAYOUT.get(role, DEFAULT_BOX)
            cutout_url = item.get("cutout_image_url")
            thumb_url = item.get("thumbnail_url")

            img = None
            if cutout_url:
                img = await _download_image(cutout_url)
            if img is None and thumb_url:
                img = await _download_image(thumb_url)

            if img is not None:
                _paste_item(canvas, img, box)
            else:
                _draw_placeholder(canvas, box, role)

        rgb = canvas.convert("RGB")
        buf = io.BytesIO()
        rgb.save(buf, format="JPEG", quality=85)
        buf.seek(0)

        from app.bootstrap.container import object_storage
        storage = object_storage()
        key = f"{snapshot.user_id}/outfit_boards/{asset_id}.jpg"
        image_url = await storage.put_object(key, buf.read(), "image/jpeg")

        async with AsyncSessionLocal() as session:
            repo = SqlAlchemyOutfitVisualRepository(session)
            await repo.update_status(snapshot.id, "completed", image_url=image_url)
            await session.commit()

        logger.info("outfit_board_generated", asset_id=asset_id, image_url=image_url)

    except Exception as exc:
        logger.exception("outfit_board_failed", asset_id=asset_id, error=str(exc))
        async with AsyncSessionLocal() as session:
            repo = SqlAlchemyOutfitVisualRepository(session)
            await repo.update_status(snapshot.id, "failed", error=str(exc))
            await session.commit()
