"""OOTD poster generation — V4 vertical slice on `job_runtime`.

Turns a user's real wear selfie into a shareable stylized poster by repainting
ONLY the background (wanx mask edit), keeping the person + clothes pixel-locked.
That's the hallucination-control guarantee: the model can't alter real garments.

Mirrors `outfit_board_worker` exactly for the async-jobs plumbing (idempotent
enqueue, claim → process → finalize with lease fencing, dead-letter reconcile).
The render step differs: download selfie → human-seg mask → wanx repaint →
watermark → upload.

Producers (the POST /v1/ootd-posters endpoint) insert an `OutfitVisualAsset`
row (`visual_type='ootd_poster'`, status `queued`) and call `enqueue_ootd_poster`
in the same transaction. Each user request is a fresh asset → fresh poster, so
idempotency is per-asset (no cross-request dedup like outfit boards).
"""

from __future__ import annotations

import io
import uuid

import httpx
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.importers.link_parser import BROWSER_HEADERS, is_safe_url
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.workers._visual_asset_runtime import (
    VisualAssetSnapshot,
    drain_visual_jobs,
    reconcile_dead_visual_assets,
)
from app.infrastructure.workers.job_runtime import enqueue

logger = structlog.get_logger(__name__)

JOB_TYPE = "ootd_poster"
MAX_DRAIN_PER_TICK = 3  # external gen API is slow (~10-15s) + rate-limited

# Background style templates → repaint prompt. The mask locks the subject, so
# the prompt only ever describes the background.
BACKGROUND_PROMPTS: dict[str, str] = {
    "minimal": "极简纯色摄影棚背景，柔和自然光，米白色调，干净通透，杂志感",
    "studio": "专业摄影棚背景，柔和打光，浅灰色背景，质感细腻",
    "street": "城市街拍背景，自然日光，虚化街景，时尚杂志风格",
}
DEFAULT_TEMPLATE = "minimal"

# On-image notice. Clients ALSO get a zh disclaimer field; the watermark is the
# durable, screenshot-proof copy of "this is AI, clothes are real".
WATERMARK_TEXT = "AI preview - fit & texture per real garment"


async def enqueue_ootd_poster(
    session: AsyncSession,
    *,
    asset_id: uuid.UUID,
    user_id: uuid.UUID | None = None,
    correlation_id: str | None = None,
) -> tuple[AsyncJob, bool]:
    """Idempotent (per-asset) enqueue. Caller owns commit and must have already
    inserted the matching `OutfitVisualAsset` row."""
    return await enqueue(
        session,
        job_type=JOB_TYPE,
        idempotency_key=f"ootd_poster:asset:{asset_id}",
        payload={"asset_id": str(asset_id)},
        user_id=user_id,
        target_id=asset_id,
        correlation_id=correlation_id,
    )


async def _download_bytes(url: str) -> bytes:
    if not is_safe_url(url):
        raise ValueError(f"unsafe url blocked: {url}")
    async with httpx.AsyncClient(timeout=30, headers=BROWSER_HEADERS) as client:
        resp = await client.get(url, follow_redirects=True)
        resp.raise_for_status()
        return resp.content


def _apply_watermark(image_bytes: bytes) -> bytes:
    from PIL import Image, ImageDraw, ImageFont

    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    draw = ImageDraw.Draw(img)
    font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), WATERMARK_TEXT, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    pad = 8
    x = img.width - tw - pad
    y = img.height - th - pad
    draw.rectangle([x - pad, y - pad, img.width, img.height], fill=(0, 0, 0))
    draw.text((x, y), WATERMARK_TEXT, fill=(255, 255, 255), font=font)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=88)
    return out.getvalue()


async def _render_and_upload(snapshot: VisualAssetSnapshot) -> tuple[str, str]:
    """Selfie → human-seg mask → wanx background repaint → watermark → upload.
    Returns `(image_url, object_key)`. Raises on any failure (worker converts
    to mark_failed)."""
    from app.bootstrap.container import image_generation_service, object_storage
    from app.infrastructure.image.human_mask import build_subject_mask

    snap = snapshot.input_snapshot
    source_url = snap.get("ootd_image_url")
    if not source_url:
        raise ValueError("missing ootd_image_url in input_snapshot")
    # `or` keeps template_id a str (mypy) and falls back on missing/empty.
    template_id = snap.get("template_id") or DEFAULT_TEMPLATE
    prompt = BACKGROUND_PROMPTS.get(template_id, BACKGROUND_PROMPTS[DEFAULT_TEMPLATE])

    base_bytes = await _download_bytes(source_url)
    mask_bytes = build_subject_mask(base_bytes)
    generated = await image_generation_service().edit_with_mask(
        base_image=base_bytes, mask_image=mask_bytes, prompt=prompt
    )
    final_bytes = _apply_watermark(generated)

    storage = object_storage()
    key = f"{snapshot.user_id}/ootd_posters/{snapshot.id}.jpg"
    url = await storage.put_object(key, final_bytes, "image/jpeg")
    return url, key


async def reconcile_dead_ootd_poster_assets(ctx: object) -> int:
    return await reconcile_dead_visual_assets(JOB_TYPE, "ootd_poster")


async def drain_ootd_poster_jobs(ctx: object) -> int:
    return await drain_visual_jobs(
        JOB_TYPE,
        _render_and_upload,
        MAX_DRAIN_PER_TICK,
        "ootd_poster",
        asset_not_found_event="ootd_poster_asset_not_found",
    )
