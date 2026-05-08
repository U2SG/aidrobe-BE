"""Account self-service router — DELETE /v1/me (Phase E1).

Separate from /v1/auth because deactivation is a self-service product action,
not an authentication concern. The endpoint is gated by `ENABLE_USER_PURGE`
so a kill switch exists for incidents (e.g. an iOS bug accidentally calling
DELETE on every cold start)."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.cost_guard import cost_summary_today
from app.application.users.deactivate_account import deactivate_account
from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.config.settings import Settings, get_settings
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
    await session.commit()

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
