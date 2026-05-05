"""Outfit board image generation worker."""

import io
import uuid

import httpx
import structlog
from PIL import Image, ImageDraw

from app.infrastructure.persistence.session import AsyncSessionLocal

logger = structlog.get_logger(__name__)

# Canvas dimensions
CANVAS_W, CANVAS_H = 800, 1000
PLACEHOLDER_COLOR = (220, 220, 220)

# Layout boxes: (x, y, w, h) for each role
LAYOUT: dict[str, tuple[int, int, int, int]] = {
    "outerwear": (250, 20, 300, 280),
    "top": (40, 320, 280, 280),
    "bag": (480, 320, 240, 240),
    "bottom": (200, 610, 320, 280),
    "dress": (200, 320, 320, 440),
    "shoes": (280, 880, 240, 100),
}
DEFAULT_BOX = (200, 400, 320, 320)


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
    # Centre in box
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
    draw.text((x + w // 2, y + h // 2), label[:12], fill=(150, 150, 150), anchor="mm")


async def generate_outfit_board(ctx: object, asset_id: str) -> None:
    from app.infrastructure.persistence.repositories.outfit_visual_repo import (
        SqlAlchemyOutfitVisualRepository,
    )

    async with AsyncSessionLocal() as session:
        repo = SqlAlchemyOutfitVisualRepository(session)
        asset = await repo.get_by_id(uuid.UUID(asset_id))
        if asset is None:
            logger.warning("outfit_visual_asset_not_found", asset_id=asset_id)
            return

        await repo.update_status(asset.id, "processing")
        await session.commit()

    try:
        snapshot = asset.input_snapshot or {}
        items = snapshot.get("item_list", [])

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

        # Convert to RGB for JPEG
        rgb = canvas.convert("RGB")
        buf = io.BytesIO()
        rgb.save(buf, format="JPEG", quality=85)
        buf.seek(0)

        # Upload to storage
        from app.bootstrap.container import object_storage
        storage = object_storage()

        user_id = str(asset.user_id)
        key = f"{user_id}/outfit_boards/{asset_id}.jpg"

        if hasattr(storage, "upload_bytes"):
            image_url = await storage.upload_bytes(buf.read(), key, "image/jpeg")
        else:
            # Fallback: use presign + store locally
            presign = await storage.create_presigned_upload(
                f"{asset_id}.jpg", "image/jpeg", 5 * 1024 * 1024
            )
            image_url = presign.file_url

        async with AsyncSessionLocal() as session:
            repo = SqlAlchemyOutfitVisualRepository(session)
            await repo.update_status(asset.id, "completed", image_url=image_url)
            await session.commit()

        logger.info("outfit_board_generated", asset_id=asset_id, image_url=image_url)

    except Exception as exc:
        logger.exception("outfit_board_failed", asset_id=asset_id, error=str(exc))
        async with AsyncSessionLocal() as session:
            repo = SqlAlchemyOutfitVisualRepository(session)
            await repo.update_status(asset.id, "failed", error=str(exc))
            await session.commit()
