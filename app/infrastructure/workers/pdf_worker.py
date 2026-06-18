"""PDF / magazine style ingest — vertical slice on `job_runtime`.

M19: a user uploads a PDF magazine (OSS direct upload via the existing presign
flow); we rasterize it to per-page images, keep the outfit pages, and feed each
kept page through the EXISTING style-learn path so it lands in the recommender
exactly like a normal inspiration image.

Model (no new table):
- Parent `UserStyleProfile` row, `source_type='pdf_magazine'`, `learn_status`
  driving the read model. Its id is the user-visible `source_id`. The PDF URL
  lives in `source_url`.
- One child `UserStyleProfile` per kept page, `source_type='inspiration_image'`
  (deliberately the existing type so it flows through whatever already consumes
  inspirations), `source_id`=parent id, `image_url`=the page image URL.

Pipeline (drain side):
  claim job → flip parent to processing → rasterize PDF (pdf2image, guarded) →
  filter blank/ad pages → per kept page: create child row + `learn_style_source`
  → aggregate count → parent.learn_status='learned' → mark_succeeded.

Reuse: per-page style analysis is `learn_style_source` (the same call the
inspiration endpoint makes). This worker does NOT reimplement style analysis.

Idempotency: key is `pdf_ingest:{parent_id}`. A lease-recovery reclaim mid-PDF
could otherwise duplicate child rows, so before creating children we skip if any
child already references the parent (at-most-once best effort — see PR note).

Graceful degradation: pdf2image / poppler is an optional system dependency. The
import is guarded; if it is unavailable the job fails cleanly (retry → dead) and
the app still boots and tests still run without poppler.
"""

from __future__ import annotations

import io
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.style import UserStyleProfile
from app.infrastructure.persistence.session import AsyncSessionLocal
from app.infrastructure.workers.job_runtime import (
    claim_one,
    enqueue,
    mark_failed,
    mark_succeeded,
)
from app.infrastructure.workers.style_learn_worker import learn_style_source

logger = structlog.get_logger(__name__)

JOB_TYPE = "pdf_ingest"
# Magazines are heavy (download + rasterize N pages). Drain one at a time per
# tick so a 48-page PDF doesn't starve the other arq queues.
MAX_DRAIN_PER_TICK = 1

# Rasterization DPI. Low enough to keep memory bounded for big magazines, high
# enough that downstream vision/heuristics still get a usable image.
RENDER_DPI = 120
# A fully-rendered page below this byte size is almost certainly blank or a
# near-empty ad filler — cheap blank-page filter without pulling in CV deps.
MIN_PAGE_BYTES = 6_000


def _idempotency_key(parent_id: uuid.UUID) -> str:
    return f"pdf_ingest:{parent_id}"


async def enqueue_pdf_ingest(
    session: AsyncSession,
    *,
    parent_id: uuid.UUID,
    file_url: str,
    user_id: uuid.UUID | None = None,
    correlation_id: str | None = None,
) -> tuple[AsyncJob, bool]:
    """Idempotent enqueue for a PDF ingest job. Caller owns commit and is
    responsible for having already inserted the parent `UserStyleProfile`."""
    return await enqueue(
        session,
        job_type=JOB_TYPE,
        idempotency_key=_idempotency_key(parent_id),
        payload={"parent_id": str(parent_id), "file_url": file_url},
        user_id=user_id,
        target_id=parent_id,
        correlation_id=correlation_id,
    )


MAX_PDF_BYTES = 50 * 1024 * 1024  # 50 MB


async def _download_pdf(url: str) -> bytes:
    from app.infrastructure.importers.link_parser import _is_safe_url

    if not _is_safe_url(url):
        raise ValueError(f"unsafe file_url blocked: {url}")

    async with httpx.AsyncClient(timeout=30) as client:
        async with client.stream("GET", url) as resp:
            resp.raise_for_status()
            chunks: list[bytes] = []
            total = 0
            async for chunk in resp.aiter_bytes(chunk_size=65536):
                total += len(chunk)
                if total > MAX_PDF_BYTES:
                    raise ValueError(f"PDF exceeds size limit ({MAX_PDF_BYTES // 1024 // 1024} MB)")
                chunks.append(chunk)
    return b"".join(chunks)


def _rasterize_pdf(pdf_bytes: bytes, *, dpi: int = RENDER_DPI) -> list[bytes]:
    """PDF bytes → one JPEG per page. Guarded import: pdf2image needs the
    poppler binary, an optional system dep. If it's missing we raise so the
    job retries/dies cleanly rather than taking down import-time."""
    try:
        from pdf2image import convert_from_bytes
    except Exception as exc:  # ImportError or poppler-not-found at import
        raise RuntimeError(f"pdf2image_unavailable: {exc}") from exc

    pages = convert_from_bytes(pdf_bytes, dpi=dpi, fmt="jpeg")
    out: list[bytes] = []
    for page in pages:
        buf = io.BytesIO()
        page.convert("RGB").save(buf, format="JPEG", quality=85)
        out.append(buf.getvalue())
    return out


def _is_outfit_page(image_bytes: bytes) -> bool:
    """Cheap blank/ad-page filter. The plan calls for blur + outfit-detection
    confidence > 0.5; that needs a CV model we deliberately don't pull in for
    V4 M19. Byte-size is a conservative proxy: blank/near-empty pages compress
    to almost nothing at this DPI. Real outfit detection is a follow-up."""
    return len(image_bytes) >= MIN_PAGE_BYTES


@dataclass
class _ParentSnapshot:
    id: uuid.UUID
    user_id: uuid.UUID
    file_url: str


async def _upload_page(user_id: uuid.UUID, parent_id: uuid.UUID, idx: int, data: bytes) -> str:
    """Persist a rendered page to object storage and return its render URL."""
    from app.bootstrap.container import object_storage

    storage = object_storage()
    key = f"{user_id}/style_pdf/{parent_id}/page_{idx:03d}.jpg"
    return await storage.put_object(key, data, "image/jpeg")


async def _children_exist(session: AsyncSession, parent_id: uuid.UUID) -> bool:
    count = await session.scalar(
        select(func.count()).select_from(UserStyleProfile).where(UserStyleProfile.source_id == parent_id)
    )
    return bool(count)


async def _process_one(job: AsyncJob) -> None:
    """Run a single claimed pdf_ingest job end-to-end. Short-lived sessions so a
    crash between steps doesn't poison the next claim."""
    payload: dict[str, Any] = job.payload or {}
    parent_id_str = payload.get("parent_id")
    file_url = payload.get("file_url")
    if not parent_id_str or not file_url:
        async with AsyncSessionLocal() as session:
            await mark_failed(
                session,
                job.id,
                claimed_attempt=job.attempts,
                error="missing parent_id/file_url in payload",
            )
            await session.commit()
        return

    parent_id = uuid.UUID(parent_id_str)
    structlog.contextvars.bind_contextvars(
        correlation_id=job.correlation_id or "",
        job_id=str(job.id),
        parent_id=str(parent_id),
    )
    try:
        # 1. Snapshot parent + flip read-model status to processing.
        async with AsyncSessionLocal() as session:
            parent = await session.get(UserStyleProfile, parent_id)
            if parent is None:
                logger.warning("pdf_ingest_parent_not_found", parent_id=str(parent_id))
                async with AsyncSessionLocal() as fail_session:
                    await mark_failed(fail_session, job.id, claimed_attempt=job.attempts, error="parent_not_found")
                    await fail_session.commit()
                return
            snapshot = _ParentSnapshot(id=parent.id, user_id=parent.user_id, file_url=file_url)
            already_ingested = await _children_exist(session, parent_id)
            parent.learn_status = "processing"
            await session.commit()

        # 2. Download + rasterize + filter + per-page learn (no DB session held
        #    during slow IO). Skipped if a prior attempt already created the
        #    children (lease-recovery reclaim) — at-most-once best effort.
        kept_pages = 0
        if not already_ingested:
            try:
                pdf_bytes = await _download_pdf(snapshot.file_url)
                page_images = _rasterize_pdf(pdf_bytes)
            except Exception as exc:
                logger.warning("pdf_ingest_rasterize_failed", error=str(exc))
                async with AsyncSessionLocal() as fail_session:
                    new_status = await mark_failed(fail_session, job.id, claimed_attempt=job.attempts, error=str(exc))
                    if new_status == "dead":
                        parent = await fail_session.get(UserStyleProfile, parent_id)
                        if parent is not None:
                            parent.learn_status = "failed"
                    await fail_session.commit()
                return

            outfit_pages = [img for img in page_images if _is_outfit_page(img)]
            for idx, page_bytes in enumerate(outfit_pages):
                try:
                    image_url = await _upload_page(snapshot.user_id, parent_id, idx, page_bytes)
                except Exception as exc:
                    # A single page upload failing shouldn't abort the whole
                    # magazine — log and skip it.
                    logger.warning("pdf_ingest_page_upload_failed", page_index=idx, error=str(exc))
                    continue
                async with AsyncSessionLocal() as session:
                    child = UserStyleProfile(
                        user_id=snapshot.user_id,
                        source_type="inspiration_image",
                        source_id=parent_id,
                        image_url=image_url,
                        weight=1.0,
                        learn_status="queued",
                    )
                    session.add(child)
                    await session.flush()
                    child_id = child.id
                    await session.commit()
                # Reuse the existing per-image style-learn path verbatim.
                await learn_style_source(None, str(child_id))
                kept_pages += 1

        # 3. Finalize. Fenced on this attempt's claim: if the lease was recovered
        #    while we were rasterizing (big PDFs run long), `mark_succeeded`
        #    returns False and we MUST NOT flip the parent to learned — another
        #    worker may have taken over the recovered job.
        async with AsyncSessionLocal() as session:
            parent = await session.get(UserStyleProfile, parent_id)
            if parent is not None:
                parent.learn_status = "learned"
                parent.learned_summary = {"kept_pages": kept_pages, "source": "pdf_magazine"}
            ok = await mark_succeeded(
                session,
                job.id,
                claimed_attempt=job.attempts,
                result={"kept_pages": kept_pages},
            )
            if not ok:
                await session.rollback()
                logger.warning("pdf_ingest.lease_lost_discarding_completion", job_id=str(job.id))
                return
            await session.commit()
            logger.info("pdf_ingest_completed", parent_id=str(parent_id), kept_pages=kept_pages)
    finally:
        structlog.contextvars.unbind_contextvars("correlation_id", "job_id", "parent_id")


async def drain_pdf_ingest_jobs(ctx: object) -> int:
    """arq entrypoint. Drains up to `MAX_DRAIN_PER_TICK` pdf_ingest jobs.

    Each claim/finalize uses its own short transaction so a worker crash between
    calls doesn't poison the next claim."""
    processed = 0
    for _ in range(MAX_DRAIN_PER_TICK):
        async with AsyncSessionLocal() as session:
            job = await claim_one(session, [JOB_TYPE])
            # claim_one commits internally, so closing the session here is safe.
        if job is None:
            break
        await _process_one(job)
        processed += 1
    if processed:
        logger.info("pdf_ingest.drain_completed", processed=processed)
    return processed
