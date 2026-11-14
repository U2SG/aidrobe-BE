import uuid
from typing import Annotated, cast

from fastapi import APIRouter, Depends
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.wardrobe.submit_fit_feedback import SubmitFitFeedback
from app.application.wardrobe.update_fit import FitUpdateValue, UpdateWardrobeFit
from app.common.enums import FitFeedbackType, FitScene
from app.common.locale import resolve_locale
from app.infrastructure.cache.client import get_redis_client, invalidate_daily_recommendation_cache
from app.infrastructure.persistence.models.user import User, UserSettings
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)
from app.infrastructure.persistence.repositories.style_repo import SqlAlchemyStyleRepository
from app.infrastructure.persistence.repositories.wardrobe_repo import SqlAlchemyWardrobeRepository
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.deps import get_current_user
from app.presentation.api.v1.render_url import wardrobe_image_urls
from app.presentation.api.v1.routers.wardrobe import item_response
from app.presentation.api.v1.schemas.common import ResponseModel
from app.presentation.api.v1.schemas.fit import (
    FitFeedbackRequest,
    FitFeedbackResponse,
    UpdateFitRequest,
)
from app.presentation.api.v1.schemas.wardrobe import WardrobeItemResponse

router = APIRouter(tags=["fit"])


@router.patch(
    "/v1/wardrobe-items/{item_id}/fit",
    response_model=ResponseModel[WardrobeItemResponse],
)
async def update_fit(
    item_id: uuid.UUID,
    payload: UpdateFitRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    redis_client: Annotated[Redis, Depends(get_redis_client)],
) -> ResponseModel[WardrobeItemResponse]:
    updates = cast(
        dict[str, FitUpdateValue],
        payload.model_dump(exclude_unset=True, mode="json"),
    )
    item = await UpdateWardrobeFit(
        SqlAlchemyWardrobeRepository(session),
        SqlAlchemyRecommendationRepository(session),
        SqlAlchemyStyleRepository(session),
    ).execute(
        item_id,
        current_user.id,
        updates,
    )
    if "fit_status" in updates:
        await invalidate_daily_recommendation_cache(redis_client, current_user.id)
    settings_locale = await session.scalar(select(UserSettings.locale).where(UserSettings.user_id == current_user.id))
    images = await wardrobe_image_urls(session, [item])
    return ResponseModel(data=item_response(item, resolve_locale(settings_locale=settings_locale), images[item.id]))


@router.post("/v1/fit-feedback", response_model=ResponseModel[FitFeedbackResponse])
async def submit_fit_feedback(
    payload: FitFeedbackRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ResponseModel[FitFeedbackResponse]:
    feedback, fit_status = await SubmitFitFeedback(
        SqlAlchemyWardrobeRepository(session),
        session,
    ).execute(
        current_user.id,
        payload.item_id,
        payload.outfit_id,
        payload.feedback_type.value,
        payload.scene.value,
    )
    return ResponseModel(
        data=FitFeedbackResponse(
            feedback_id=feedback.id,
            item_id=payload.item_id,
            outfit_id=feedback.outfit_id,
            feedback_type=FitFeedbackType(feedback.feedback_type),
            scene=FitScene(feedback.scene),
            fit_status=fit_status,
        )
    )
