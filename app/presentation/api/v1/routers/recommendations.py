import uuid
from datetime import UTC, date, datetime
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.outfits.feedback import FeedbackOutfit
from app.application.outfits.wear import WearOutfit
from app.application.recommendations.get_today import GetTodayRecommendation
from app.common.enums import RecommendationStatus
from app.infrastructure.persistence.models.recommendation import DailyRecommendation, Outfit
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.deps import get_current_user
from app.presentation.api.v1.schemas.common import ResponseModel
from app.presentation.api.v1.schemas.recommendations import (
    FeedbackRequest,
    FeedbackResponse,
    OutfitResponse,
    RecommendationItemResponse,
    TodayRecommendationResponse,
    WearOutfitRequest,
    WearOutfitResponse,
)

router = APIRouter(prefix="/v1/recommendations", tags=["recommendations"])


def outfit_response(outfit: Outfit) -> OutfitResponse:
    return OutfitResponse(
        outfit_id=outfit.id,
        type=outfit.outfit_type,
        title=outfit.title,
        reason=outfit.reason_text,
        items=[
            RecommendationItemResponse(item_id=item.item_id, category=item.role)
            for item in sorted(outfit.items, key=lambda i: i.display_order)
        ],
    )


def today_response(daily: DailyRecommendation) -> TodayRecommendationResponse:
    guidance = None
    if daily.status == RecommendationStatus.INSUFFICIENT_ITEMS.value:
        guidance = "再加一件下装就能搭"
    return TodayRecommendationResponse(
        recommendation_date=daily.recommendation_date,
        status=daily.status,
        guidance_text=guidance,
        outfits=[
            outfit_response(outfit) for outfit in sorted(daily.outfits, key=lambda o: o.rank_order)
        ]
        if daily.id is not None
        else [],
    )


@router.get("/today", response_model=ResponseModel[TodayRecommendationResponse])
async def get_today_recommendation(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ResponseModel[TodayRecommendationResponse]:
    use_case = GetTodayRecommendation(SqlAlchemyRecommendationRepository(session))
    daily = await use_case.execute(current_user.id, datetime.now(UTC).date())
    return ResponseModel(data=today_response(daily))


@router.get("/outfits/{outfit_id}", response_model=ResponseModel[OutfitResponse])
async def get_outfit_detail(
    outfit_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ResponseModel[OutfitResponse]:
    use_case = SqlAlchemyRecommendationRepository(session)
    outfit = await use_case.get_outfit_for_user(outfit_id, current_user.id)
    if outfit is None:
        from app.common import error_codes
        from app.common.exceptions import FriendlyException

        raise FriendlyException(error_codes.NOT_FOUND, "Outfit not found", status_code=404)
    return ResponseModel(data=outfit_response(outfit))


@router.post("/outfits/{outfit_id}/wear", response_model=ResponseModel[WearOutfitResponse])
async def wear_outfit(
    outfit_id: uuid.UUID,
    payload: WearOutfitRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ResponseModel[WearOutfitResponse]:
    worn_date = payload.worn_date or date.today()
    use_case = WearOutfit(SqlAlchemyRecommendationRepository(session))
    await use_case.execute(outfit_id, current_user.id, worn_date)
    return ResponseModel(data=WearOutfitResponse(success=True))


@router.post("/outfits/{outfit_id}/feedback", response_model=ResponseModel[FeedbackResponse])
async def feedback_outfit(
    outfit_id: uuid.UUID,
    payload: FeedbackRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ResponseModel[FeedbackResponse]:
    use_case = FeedbackOutfit(SqlAlchemyRecommendationRepository(session))
    await use_case.execute(outfit_id, current_user.id, payload.feedback_type)
    return ResponseModel(data=FeedbackResponse(success=True))
