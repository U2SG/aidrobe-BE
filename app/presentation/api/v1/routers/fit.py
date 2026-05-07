import uuid
from typing import Annotated, cast

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.wardrobe.submit_fit_feedback import SubmitFitFeedback
from app.application.wardrobe.update_fit import FitUpdateValue, UpdateWardrobeFit
from app.common.enums import FitFeedbackType, FitScene
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.wardrobe_repo import SqlAlchemyWardrobeRepository
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.deps import get_current_user
from app.presentation.api.v1.schemas.common import ResponseModel
from app.presentation.api.v1.schemas.fit import (
    FitFeedbackRequest,
    FitFeedbackResponse,
    UpdateFitRequest,
)
from app.presentation.api.v1.schemas.wardrobe import WardrobeItemResponse

router = APIRouter(tags=["fit"])


def item_response(item: WardrobeItem) -> WardrobeItemResponse:
    return WardrobeItemResponse(
        item_id=item.id,
        title=item.title,
        category=item.category,
        color_main=item.color_main,
        status=item.status,
        cover_image_url=item.cover_image_url,
        original_image_url=item.original_image_url,
        location=item.location,
        size_label=item.size_label,
        size_region=item.size_region,
        fit_intent=item.fit_intent,
        fit_status=item.fit_status,
        comfort_score=item.comfort_score,
        # A-4: new fields
        last_worn=item.last_worn_at,
        purchase_date=item.purchase_date,
    )


@router.patch(
    "/v1/wardrobe-items/{item_id}/fit",
    response_model=ResponseModel[WardrobeItemResponse],
)
async def update_fit(
    item_id: uuid.UUID,
    payload: UpdateFitRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ResponseModel[WardrobeItemResponse]:
    updates = cast(
        dict[str, FitUpdateValue],
        payload.model_dump(exclude_unset=True, mode="json"),
    )
    item = await UpdateWardrobeFit(SqlAlchemyWardrobeRepository(session)).execute(
        item_id,
        current_user.id,
        updates,
    )
    return ResponseModel(data=item_response(item))


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
