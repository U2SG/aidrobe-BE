"""Recoverable true crops, independently of the recognition checkpoint."""

from __future__ import annotations

import asyncio
import hashlib
import io
import uuid
from collections.abc import Sequence
from typing import Any

import structlog
from PIL import Image, ImageOps
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.import_tasks.candidates import confirmed_item_ids, normalize_candidates, store_candidates
from app.application.import_tasks.confirm_import import apply_confirmed_crop
from app.common.enums import ImportStatus
from app.config.settings import get_settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.import_task import ImportTask
from app.infrastructure.persistence.repositories.import_task_repo import SqlAlchemyImportTaskRepository
from app.infrastructure.persistence.repositories.wardrobe_repo import SqlAlchemyWardrobeRepository
from app.infrastructure.persistence.session import AsyncSessionLocal
from app.infrastructure.storage.source_image import load_source_bytes
from app.infrastructure.vision.qwen_vision import garment_box
from app.infrastructure.workers.job_runtime import (
    claim_is_current,
    claim_one,
    enqueue,
    mark_failed,
    mark_succeeded,
    recover_expired_leases,
    release_cancelled_claim,
)

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


CROP_JOB_TYPE = "garment_crop"
CROP_ATTEMPT_TIMEOUT = 45
CROP_LEASE_SECONDS = CROP_ATTEMPT_TIMEOUT + 15
CROP_MAX_DRAIN = 12


async def enqueue_garment_crop(
    session: AsyncSession, task: ImportTask, candidate_id: str, version: str, import_job: AsyncJob | None
) -> None:
    identity = hashlib.sha256(f"{candidate_id}:{version}".encode()).hexdigest()
    await enqueue(
        session, job_type=CROP_JOB_TYPE, idempotency_key=f"{CROP_JOB_TYPE}:{task.id}:{identity}",
        user_id=task.user_id, target_id=task.id, payload={"candidate_id": candidate_id, "source_version": version},
        correlation_id=import_job.correlation_id if import_job else None,
    )


def _crop_candidate(task: ImportTask | None, job: AsyncJob) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
    payload = job.payload or {}
    if task is None or task.input_payload.get("source_version") != payload.get("source_version"):
        return None
    if task.task_status not in (ImportStatus.WAITING_CONFIRM.value, ImportStatus.COMPLETED.value):
        return None
    candidates = normalize_candidates(task)
    candidate_id = payload.get("candidate_id")
    candidate = next((candidate for candidate in candidates if candidate["temp_item_id"] == candidate_id), None)
    if candidate is None or candidate.get("should_import") is False or candidate.get("crop_object_key"):
        return None
    if task.task_status == ImportStatus.COMPLETED.value and candidate_id not in confirmed_item_ids(task):
        return None
    return candidates, candidate


async def _crop_one(session: AsyncSession, job: AsyncJob) -> None:
    from app.bootstrap.container import object_storage

    if job.target_id is None or job.user_id is None:
        raise ValueError("invalid crop identity")
    repository = SqlAlchemyImportTaskRepository(session)
    task = await repository.get_for_user(job.target_id, job.user_id)
    found = _crop_candidate(task, job)
    if found is None or task is None:
        await mark_succeeded(session, job.id, claimed_attempt=job.attempts, result={"outcome": "obsolete"})
        await session.commit()
        return
    _, candidate = found
    key = task.input_payload.get("source_object_key")
    url = task.input_payload.get("image_url")
    box = garment_box(candidate.get("bbox"))
    await session.rollback()
    if not isinstance(key, str) or not isinstance(url, str) or box is None:
        await mark_succeeded(session, job.id, claimed_attempt=job.attempts, result={"outcome": "no_usable_crop"})
        await session.commit()
        return

    source = await load_source_bytes(url, key)
    crop = (await asyncio.to_thread(crop_regions, source, [box]))[0]
    if crop is None:
        await mark_succeeded(session, job.id, claimed_attempt=job.attempts, result={"outcome": "no_usable_crop"})
        await session.commit()
        return
    # A lease recovery can overlap the SDK's in-flight upload. Each attempt
    # writes a different object, and only the current claim can adopt it.
    crop_key = f"users/{job.user_id}/wardrobe/crops/{job.target_id}/{job.id}/{job.attempts}.jpg"
    crop_url = await object_storage().put_object(crop_key, crop, "image/jpeg")

    task = await repository.get_for_user(job.target_id, job.user_id, for_update=True)
    if not await claim_is_current(session, job):
        await session.rollback()
        return
    found = _crop_candidate(task, job)
    if found is None or task is None:
        await mark_succeeded(session, job.id, claimed_attempt=job.attempts, result={"outcome": "obsolete"})
        await session.commit()
        return
    candidates, candidate = found
    item_id = confirmed_item_ids(task).get(candidate["temp_item_id"])
    if item_id:
        await apply_confirmed_crop(
            SqlAlchemyWardrobeRepository(session), user_id=job.user_id, item_id=uuid.UUID(item_id),
            original_url=url, crop_url=crop_url, crop_key=crop_key,
        )
    candidate["crop_object_key"] = crop_key
    candidate["crop_image_url"] = crop_url
    store_candidates(task, candidates)
    await mark_succeeded(session, job.id, claimed_attempt=job.attempts, result={"outcome": "cropped"})
    await session.commit()


async def _process_crop(job: AsyncJob) -> None:
    try:
        async with asyncio.timeout(CROP_ATTEMPT_TIMEOUT), AsyncSessionLocal() as session:
            await _crop_one(session, job)
    except asyncio.CancelledError:
        await release_cancelled_claim(job, AsyncSessionLocal)
        raise
    except Exception as exc:
        logger.warning(
            "garment_crop.attempt_failed", job_id=str(job.id), task_id=str(job.target_id),
            correlation_id=job.correlation_id, error_type=type(exc).__name__,
        )
        async with AsyncSessionLocal() as session:
            await mark_failed(session, job.id, claimed_attempt=job.attempts, error="garment_crop_failed")
            await session.commit()


async def drain_garment_crop_jobs(ctx: object) -> int:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + get_settings().ARQ_JOB_TIMEOUT_SECONDS - 5
    async with AsyncSessionLocal() as session:
        await recover_expired_leases(session, job_type=CROP_JOB_TYPE)
        await session.commit()
    processed = 0
    while processed < CROP_MAX_DRAIN and deadline - loop.time() >= CROP_LEASE_SECONDS:
        async with AsyncSessionLocal() as session:
            job = await claim_one(session, [CROP_JOB_TYPE], lease_seconds=CROP_LEASE_SECONDS)
        if job is None:
            break
        await _process_crop(job)
        processed += 1
    return processed
