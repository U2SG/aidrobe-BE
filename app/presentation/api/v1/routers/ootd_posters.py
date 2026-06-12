"""OOTD poster generation — V4. User-triggered, consent-gated, off by default.

POST creates an `OutfitVisualAsset` (visual_type='ootd_poster') + enqueues the
async job; clients poll the existing GET /v1/outfit-visual-assets/{asset_id}.
The generated image only repaints the background — real clothes stay locked.
"""

from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, status
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.cost_guard import assert_within_quota
from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.config.settings import get_settings
from app.infrastructure.cache.client import get_redis_client
from app.infrastructure.persistence.models.outfit_visual import OutfitVisualAsset
from app.infrastructure.persistence.models.recommendation import WearHistory
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.repositories.outfit_visual_repo import (
    SqlAlchemyOutfitVisualRepository,
)
from app.infrastructure.persistence.session import get_db_session
from app.infrastructure.workers.ootd_poster_worker import enqueue_ootd_poster
from app.presentation.api.v1.deps import get_current_user
from app.presentation.api.v1.schemas.ootd_posters import (
    CreateOotdPosterRequest,
    CreateOotdPosterResponse,
)

router = APIRouter(prefix="/v1", tags=["ootd-posters"])

DISCLAIMER_ZH = "AI 效果图仅供参考，实际版型、长度和质感以真实衣物为准"


@router.post(
    "/ootd-posters",
    response_model=CreateOotdPosterResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def create_ootd_poster(
    payload: CreateOotdPosterRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    redis_client: Annotated[Redis, Depends(get_redis_client)],
) -> CreateOotdPosterResponse:
    settings = get_settings()
    if not settings.ENABLE_OOTD_POSTER:
        raise FriendlyException(
            error_codes.FORBIDDEN, "OOTD poster generation is not enabled", status_code=503
        )
    if not payload.consent:
        raise FriendlyException(
            error_codes.FORBIDDEN,
            "explicit consent is required to send a body image to the generation model",
            status_code=403,
        )

    ootd_image_url = payload.ootd_image_url
    if ootd_image_url is None and payload.wear_history_id is not None:
        ootd_image_url = (
            await session.execute(
                select(WearHistory.ootd_image_url).where(
                    WearHistory.id == payload.wear_history_id,
                    WearHistory.user_id == current_user.id,
                )
            )
        ).scalar_one_or_none()
    if not ootd_image_url:
        raise FriendlyException(
            error_codes.VALIDATION_ERROR,
            "ootd_image_url or a wear_history_id with an OOTD photo is required",
            status_code=422,
        )

    # Cost guard BEFORE creating the asset/job — a refused request leaves no
    # row and burns no quota.
    await assert_within_quota(session, current_user.id, "ootd_poster", redis_client=redis_client)

    asset = OutfitVisualAsset(
        user_id=current_user.id,
        visual_type="ootd_poster",
        generation_status="queued",
        input_snapshot={
            "ootd_image_url": ootd_image_url,
            "template_id": payload.template_id,
            # Audit trail of consent-at-request-time (PIPL). The endpoint already
            # rejected non-consented requests above; this persists that grant.
            "consent": True,
        },
    )
    await SqlAlchemyOutfitVisualRepository(session).add(asset)

    correlation_id = structlog.contextvars.get_contextvars().get("correlation_id")
    await enqueue_ootd_poster(
        session,
        asset_id=asset.id,
        user_id=current_user.id,
        correlation_id=correlation_id if isinstance(correlation_id, str) else None,
    )
    return CreateOotdPosterResponse(
        asset_id=asset.id, generation_status="queued", disclaimer=DISCLAIMER_ZH
    )
