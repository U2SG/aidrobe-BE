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
from dataclasses import dataclass
from typing import Any

import httpx
import structlog
from PIL import Image, ImageDraw
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.session import AsyncSessionLocal
from app.infrastructure.workers.job_runtime import (
    claim_one,
    enqueue,
    mark_failed,
    mark_succeeded,
)

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


@dataclass
class _AssetSnapshot:
    id: uuid.UUID
    user_id: uuid.UUID
    input_snapshot: dict[str, Any]


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
    text = label[:12]
    bbox = draw.textbbox((0, 0), text)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    draw.text(
        (x + (w - tw) // 2, y + (h - th) // 2),
        text,
        fill=PLACEHOLDER_TEXT_COLOR,
    )


async def _render_and_upload(snapshot: _AssetSnapshot) -> str:
    """Render the composite board and upload to object storage. Returns the
    public image URL. Raises on storage / encode failures (the worker
    converts to mark_failed)."""
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
    key = f"{snapshot.user_id}/outfit_boards/{snapshot.id}.jpg"
    return await storage.put_object(key, buf.read(), "image/jpeg")


async def _process_one(job: AsyncJob) -> None:
    """Run a single claimed outfit_board job end-to-end. Each session is
    short-lived so a crash between calls doesn't poison subsequent claims."""
    from app.infrastructure.persistence.repositories.outfit_visual_repo import (
        SqlAlchemyOutfitVisualRepository,
    )

    payload: dict[str, Any] = job.payload or {}
    asset_id_str = payload.get("asset_id")
    if not asset_id_str:
        async with AsyncSessionLocal() as session:
            await mark_failed(session, job.id, claimed_attempt=job.attempts, error="missing asset_id in payload")
            await session.commit()
        return

    asset_id = uuid.UUID(asset_id_str)
    structlog.contextvars.bind_contextvars(
        correlation_id=job.correlation_id or "",
        job_id=str(job.id),
        asset_id=str(asset_id),
    )
    try:
        # 1. Snapshot input + flip read-model status to processing.
        async with AsyncSessionLocal() as session:
            repo = SqlAlchemyOutfitVisualRepository(session)
            asset = await repo.get_by_id(asset_id)
            if asset is None:
                logger.warning("outfit_visual_asset_not_found", asset_id=str(asset_id))
                async with AsyncSessionLocal() as fail_session:
                    await mark_failed(
                        fail_session,
                        job.id,
                        claimed_attempt=job.attempts,
                        error="asset_not_found",
                    )
                    await fail_session.commit()
                return
            snapshot = _AssetSnapshot(
                id=asset.id,
                user_id=asset.user_id,
                input_snapshot=dict(asset.input_snapshot or {}),
            )
            await repo.update_status(asset_id, "processing")
            await session.commit()

        # 2. Render + upload (no DB session held during slow IO).
        try:
            image_url = await _render_and_upload(snapshot)
        except Exception as exc:
            logger.warning("outfit_board_render_failed", error=str(exc))
            async with AsyncSessionLocal() as session:
                repo = SqlAlchemyOutfitVisualRepository(session)
                # Asset stays in 'processing' so a retry can flip it back; only
                # on dead does the read model land on 'failed' (handled below
                # via the runtime's status return).
                await session.commit()
            async with AsyncSessionLocal() as fail_session:
                new_status = await mark_failed(fail_session, job.id, claimed_attempt=job.attempts, error=str(exc))
                # If the runtime tells us the job is dead, surface that to the
                # read model so the GET endpoint shows 'failed' rather than
                # an indefinite 'processing'.
                if new_status == "dead":
                    repo = SqlAlchemyOutfitVisualRepository(fail_session)
                    await repo.update_status(asset_id, "failed", error=str(exc))
                await fail_session.commit()
            return

        # 3. Persist result + finalize. Fenced on this attempt's claim: if the
        # lease was recovered while we were rendering (renders can run long),
        # `mark_succeeded` returns False and we MUST NOT commit `completed`
        # to the read model. Otherwise a stale worker would expose a board
        # for a job that another retry has already taken over (or that
        # oncall sees as `dead`), and the asset would race against the
        # retry's own write.
        async with AsyncSessionLocal() as session:
            repo = SqlAlchemyOutfitVisualRepository(session)
            await repo.update_status(asset_id, "completed", image_url=image_url)
            ok = await mark_succeeded(session, job.id, claimed_attempt=job.attempts)
            if not ok:
                await session.rollback()
                logger.warning(
                    "outfit_board.lease_lost_discarding_completion",
                    job_id=str(job.id),
                    asset_id=str(asset_id),
                )
                return
            await session.commit()
            logger.info(
                "outfit_board_generated",
                asset_id=str(asset_id),
                image_url=image_url,
            )
    finally:
        structlog.contextvars.unbind_contextvars("correlation_id", "job_id", "asset_id")


async def reconcile_dead_outfit_board_assets(ctx: object) -> int:
    """Flip `OutfitVisualAsset.generation_status` to 'failed' for assets whose
    backing job has gone `dead` via lease recovery (which happens when a
    worker crashes hard enough to never reach `mark_failed` itself).

    Without this, a hard-killed render leaves the asset stuck on
    `processing` forever — oncall sees the job as `dead` but the user-facing
    GET endpoint still says "still rendering."

    Runs as an arq cron alongside `recover_async_job_leases` so the user-
    visible read model converges within one recovery interval."""
    from sqlalchemy import select

    from app.infrastructure.persistence.models.async_job import AsyncJob
    from app.infrastructure.persistence.models.outfit_visual import OutfitVisualAsset
    from app.infrastructure.persistence.repositories.outfit_visual_repo import (
        SqlAlchemyOutfitVisualRepository,
    )

    async with AsyncSessionLocal() as session:
        # All async_jobs of type outfit_board that are dead but whose target
        # asset is still in `queued` / `processing`.
        rows = (
            await session.execute(
                select(AsyncJob.id, AsyncJob.target_id, AsyncJob.last_error)
                .join(OutfitVisualAsset, OutfitVisualAsset.id == AsyncJob.target_id)
                .where(
                    AsyncJob.job_type == JOB_TYPE,
                    AsyncJob.status == "dead",
                    AsyncJob.target_id.is_not(None),
                    OutfitVisualAsset.generation_status.in_(("queued", "processing")),
                )
            )
        ).all()

        if not rows:
            return 0

        repo = SqlAlchemyOutfitVisualRepository(session)
        for _job_id, target_id, last_error in rows:
            await repo.update_status(target_id, "failed", error=last_error or "job_dead_letter")
        await session.commit()
    logger.warning("outfit_board.reconciled_dead_assets", count=len(rows))
    return len(rows)


async def drain_outfit_board_jobs(ctx: object) -> int:
    processed = 0
    for _ in range(MAX_DRAIN_PER_TICK):
        async with AsyncSessionLocal() as session:
            job = await claim_one(session, [JOB_TYPE])
        if job is None:
            break
        await _process_one(job)
        processed += 1
    if processed:
        logger.info("outfit_board.drain_completed", processed=processed)
    return processed
