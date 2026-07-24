"""Admin observability surface — Phase C deliverable.

Currently exposes the async-jobs dead-letter view so oncall has a path to
inspect dead jobs without shelling into the DB. Gated by `require_admin_user`
which fails closed in prod when `ADMIN_USER_IDS` is unset.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports.llm import LlmPort
from app.application.wardrobe.backfill_names import backfill_missing_localized_names
from app.bootstrap.container import llm_service
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.deps import require_admin_user

router = APIRouter(prefix="/v1/admin", tags=["admin"])

_ALLOWED_STATUSES = {"queued", "running", "succeeded", "failed", "dead"}


class AdminAsyncJobItem(BaseModel):
    job_id: uuid.UUID
    job_type: str
    status: str
    attempts: int
    max_attempts: int
    last_error: str | None = None
    correlation_id: str | None = None
    user_id: uuid.UUID | None = None
    target_id: uuid.UUID | None = None
    cost_tokens: int = 0
    cost_cents: int = 0
    created_at: datetime
    updated_at: datetime
    next_run_at: datetime
    lease_expires_at: datetime | None = None

    model_config = {"from_attributes": True}


class AdminAsyncJobListResponse(BaseModel):
    items: list[AdminAsyncJobItem]
    total: int
    limit: int
    offset: int


@router.get("/jobs", response_model=AdminAsyncJobListResponse)
async def list_async_jobs(
    _admin: Annotated[User, Depends(require_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    status: Annotated[str, Query(description="Job status filter")] = "dead",
    job_type: Annotated[str | None, Query(description="Filter to one job_type")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AdminAsyncJobListResponse:
    """List async jobs filtered by status (default `dead`).

    Bounded by `limit<=200` so a single misconfigured query can't pull the
    whole table into memory."""
    if status not in _ALLOWED_STATUSES:
        # Treat unknown filter values as no-op rather than 422 — keeps oncall
        # tooling robust if someone hits the endpoint with a typo.
        status = "dead"

    stmt = select(AsyncJob).where(AsyncJob.status == status)
    count_stmt = select(func.count()).select_from(AsyncJob).where(AsyncJob.status == status)
    if job_type:
        stmt = stmt.where(AsyncJob.job_type == job_type)
        count_stmt = count_stmt.where(AsyncJob.job_type == job_type)

    total = (await db.execute(count_stmt)).scalar_one()
    rows = (
        await db.execute(
            stmt.order_by(AsyncJob.updated_at.desc()).limit(limit).offset(offset)
        )
    ).scalars().all()

    return AdminAsyncJobListResponse(
        items=[
            AdminAsyncJobItem(
                job_id=row.id,
                job_type=row.job_type,
                status=row.status,
                attempts=row.attempts,
                max_attempts=row.max_attempts,
                last_error=row.last_error,
                correlation_id=row.correlation_id,
                user_id=row.user_id,
                target_id=row.target_id,
                cost_tokens=row.cost_tokens,
                cost_cents=row.cost_cents,
                created_at=row.created_at,
                updated_at=row.updated_at,
                next_run_at=row.next_run_at,
                lease_expires_at=row.lease_expires_at,
            )
            for row in rows
        ],
        total=total,
        limit=limit,
        offset=offset,
    )


class BackfillItemNamesResponse(BaseModel):
    scanned: int
    updated: int
    remaining: int


@router.post("/backfill-item-names", response_model=BackfillItemNamesResponse)
async def backfill_item_names(
    _admin: Annotated[User, Depends(require_admin_user)],
    db: Annotated[AsyncSession, Depends(get_db_session)],
    llm: Annotated[LlmPort, Depends(llm_service)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> BackfillItemNamesResponse:
    """One-shot repair for legacy wardrobe items imported before naming
    localization (BE#30): AI-named rows without `localized_names` render their
    raw (usually English) title on every surface. Each call batches `limit`
    items into one LLM request producing zh-CN/en-US name pairs; user-renamed
    items are never touched. Call repeatedly until `remaining` is 0."""
    result = await backfill_missing_localized_names(db, llm, limit=limit)
    return BackfillItemNamesResponse(
        scanned=result.scanned, updated=result.updated, remaining=result.remaining
    )
