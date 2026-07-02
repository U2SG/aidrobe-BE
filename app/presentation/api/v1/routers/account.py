"""Account self-service router — DELETE /v1/me (Phase E1).

Separate from /v1/auth because deactivation is a self-service product action,
not an authentication concern. The endpoint is gated by `ENABLE_USER_PURGE`
so a kill switch exists for incidents (e.g. an iOS bug accidentally calling
DELETE on every cold start)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from sqlalchemy import select

from app.application.cost_guard import cost_summary_today
from app.application.users.deactivate_account import deactivate_account
from app.application.users.export_data import EXPORT_JOB_TYPE, request_data_export
from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.deps import get_current_user
from app.presentation.api.v1.schemas.common import ResponseModel

router = APIRouter(prefix="/v1", tags=["account"])


class DeactivateResponse(BaseModel):
    deactivated_at: str
    scheduled_purge_at: str
    grace_days: int


class CostSummaryItem(BaseModel):
    used: int
    limit: int
    remaining: int


class CostSummaryResponse(BaseModel):
    llm_ask: CostSummaryItem
    ocr_receipt: CostSummaryItem
    tryon: CostSummaryItem


@router.get("/user/cost-summary", response_model=ResponseModel[CostSummaryResponse])
async def get_cost_summary(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ResponseModel[CostSummaryResponse]:
    """User-visible token / call usage for today. Always returns a payload —
    when ENABLE_COST_GUARD is off the `limit` field is the unused default
    (so the client can pre-render the UI before the operator turns the
    feature on)."""
    raw = await cost_summary_today(session, current_user.id)
    return ResponseModel(
        data=CostSummaryResponse(
            llm_ask=CostSummaryItem(**raw["llm_ask"]),
            ocr_receipt=CostSummaryItem(**raw["ocr_receipt"]),
            tryon=CostSummaryItem(**raw["tryon"]),
        )
    )


class DataExportResponse(BaseModel):
    job_id: uuid.UUID
    status: str
    download_url: str | None = None
    message: str


@router.post("/me/data-export", response_model=ResponseModel[DataExportResponse])
async def request_my_data_export(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ResponseModel[DataExportResponse]:
    """PIPL DSAR — kick off an asynchronous export of the user's personal
    data. Bundle path runs as a `user_export` async-job; the user receives
    a download link (push / email) when ready. Idempotent per pending
    request: re-calling while one is in flight returns the same job_id."""
    job_id = await request_data_export(session, current_user)
    # No explicit commit — get_db_session commits on yield-exit.
    return ResponseModel(
        data=DataExportResponse(
            job_id=job_id,
            status="pending",
            message="Data export started. You'll be notified when it's ready.",
        )
    )


@router.get(
    "/me/data-export/{job_id}",
    response_model=ResponseModel[DataExportResponse],
)
async def get_data_export_status(
    job_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ResponseModel[DataExportResponse]:
    """Poll the status of a previously requested data export job.
    Returns 404 when the job_id does not belong to this user."""
    job = await session.scalar(
        select(AsyncJob).where(
            AsyncJob.id == job_id,
            AsyncJob.user_id == current_user.id,
            AsyncJob.job_type == EXPORT_JOB_TYPE,
        )
    )
    if job is None:
        raise FriendlyException(error_codes.NOT_FOUND, "Export job not found", status_code=404)

    download_url: str | None = None
    if job.status == "succeeded" and job.result:
        download_url = job.result.get("download_url")

    status_label = job.status  # queued | running | succeeded | failed | dead
    if job.status in ("queued", "running"):
        message = "Your export is being prepared."
    elif job.status == "succeeded":
        message = "Export ready. Download link expires in 24 hours."
    else:
        message = "Export failed. Please request a new export."

    return ResponseModel(
        data=DataExportResponse(
            job_id=job.id,
            status=status_label,
            download_url=download_url,
            message=message,
        )
    )


@router.delete("/me", response_model=ResponseModel[DeactivateResponse])
async def deactivate_me(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    settings: Annotated[Settings, Depends(get_settings)],
) -> ResponseModel[DeactivateResponse]:
    """PIPL-aware account deactivation. Soft-deletes immediately; hard-purge
    fires after the grace window via the user_purge async-job worker.

    Idempotent — calling twice on an already-deactivated account is OK and
    returns the same scheduled_purge_at."""
    if not settings.ENABLE_USER_PURGE:
        # Kill switch — operator must opt in. Default-off is deliberate so
        # a misconfigured prod deployment doesn't accept self-destruct
        # requests until the team explicitly turns it on.
        raise FriendlyException(
            error_codes.FORBIDDEN,
            "account deactivation is not enabled",
            status_code=503,
        )

    user = await deactivate_account(session, current_user)
    # No explicit commit — get_db_session commits on yield-exit.

    from datetime import timedelta

    assert user.deactivated_at is not None
    purge_at = user.deactivated_at + timedelta(days=settings.USER_PURGE_GRACE_DAYS)
    return ResponseModel(
        data=DeactivateResponse(
            deactivated_at=user.deactivated_at.isoformat(),
            scheduled_purge_at=purge_at.isoformat(),
            grace_days=settings.USER_PURGE_GRACE_DAYS,
        )
    )
