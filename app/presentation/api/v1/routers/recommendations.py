import uuid
from datetime import UTC, date, datetime, time
from typing import Annotated

import structlog
from fastapi import APIRouter, Depends, Query
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.outfits.feedback import FeedbackOutfit
from app.application.outfits.wear import WearOutfit
from app.application.recommendations.get_today import GetTodayRecommendation
from app.common import error_codes
from app.common.enums import RecommendationStatus, TimeSensitivity
from app.common.exceptions import FriendlyException
from app.infrastructure.cache.client import get_redis_client
from app.infrastructure.cache.keys import build_daily_recommendation_cache_key
from app.infrastructure.persistence.models.recommendation import DailyRecommendation, Outfit
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.repositories.operation_log_repo import (
    SqlAlchemyOperationLogRepository,
)
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)
from app.infrastructure.persistence.repositories.user_repo import SqlAlchemyUserRepository
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.deps import get_current_user
from app.presentation.api.v1.schemas.common import ResponseModel
from app.presentation.api.v1.schemas.recommendations import (
    FeedbackRequest,
    FeedbackResponse,
    OutfitHistoryItemResponse,
    OutfitHistoryResponse,
    OutfitResponse,
    RecommendationItemResponse,
    TodayRecommendationResponse,
    WearOutfitRequest,
    WearOutfitResponse,
)

router = APIRouter(prefix="/v1/recommendations", tags=["recommendations"])
logger = structlog.get_logger(__name__)


def outfit_response(outfit: Outfit) -> OutfitResponse:
    return OutfitResponse(
        outfit_id=outfit.id,
        type=outfit.outfit_type,
        title=outfit.title,
        reason=outfit.reason_text,
        time_sensitivity=outfit.time_sensitivity,
        items=[
            RecommendationItemResponse(item_id=item.item_id, category=item.role)
            for item in sorted(outfit.items, key=lambda i: i.display_order)
        ],
    )


def today_response(
    daily: DailyRecommendation,
    departure_time: time | None = None,
    scene: str | None = None,
) -> TodayRecommendationResponse:
    guidance = None
    if daily.status == RecommendationStatus.INSUFFICIENT_ITEMS.value:
        guidance = "再加一件下装就能搭"
    outfits = sorted(daily.outfits, key=lambda o: o.rank_order)
    selected_outfits = _select_outfits_for_remaining_time(outfits, departure_time)
    selected_outfits = _select_outfits_for_scene(selected_outfits, scene)
    return TodayRecommendationResponse(
        recommendation_date=daily.recommendation_date,
        status=daily.status,
        guidance_text=guidance,
        outfits=[outfit_response(outfit) for outfit in selected_outfits] if daily.id is not None else [],
    )


def _select_outfits_for_remaining_time(
    outfits: list[Outfit], departure_time: time | None
) -> list[Outfit]:
    if departure_time is None:
        return outfits[:3]
    now = datetime.now(UTC).time()
    remaining_minutes = (departure_time.hour * 60 + departure_time.minute) - (
        now.hour * 60 + now.minute
    )
    if remaining_minutes < 0:
        remaining_minutes += 24 * 60
    if remaining_minutes < 15:
        instant = [outfit for outfit in outfits if outfit.time_sensitivity == TimeSensitivity.RUSHED.value]
        return (instant or outfits)[:1]
    if remaining_minutes < 30:
        preferred = [
            outfit
            for outfit in outfits
            if outfit.time_sensitivity
            in {TimeSensitivity.RUSHED.value, TimeSensitivity.STANDARD.value}
        ]
        return (preferred or outfits)[:3]
    if remaining_minutes <= 90:
        preferred = [
            outfit for outfit in outfits if outfit.time_sensitivity != TimeSensitivity.RELAXED.value
        ]
        return (preferred or outfits)[:3]
    return outfits[:3]


def _select_outfits_for_scene(outfits: list[Outfit], scene: str | None) -> list[Outfit]:
    if scene == "formal":
        preferred = [outfit for outfit in outfits if outfit.outfit_type != "fast"]
        return preferred or outfits
    if scene == "casual":
        preferred = [outfit for outfit in outfits if outfit.outfit_type != "safe"]
        return preferred or outfits
    return outfits


def _seconds_until_utc_day_end(now: datetime) -> int:
    end_of_day = datetime.combine(now.date(), time(23, 59, 59), tzinfo=UTC)
    return max(1, int((end_of_day - now).total_seconds()))


@router.get("/today", response_model=ResponseModel[TodayRecommendationResponse])
async def get_today_recommendation(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    redis_client: Annotated[Redis, Depends(get_redis_client)],
    scene: str | None = Query(default=None, pattern="^(commute|formal|casual)$"),
) -> ResponseModel[TodayRecommendationResponse]:
    now = datetime.now(UTC)
    today = now.date()
    settings = await SqlAlchemyUserRepository(session).get_or_create_settings(current_user.id)
    cache_key = build_daily_recommendation_cache_key(
        current_user.id,
        today,
        settings.departure_time,
        scene,
    )
    try:
        cached = await redis_client.get(cache_key)
    except RedisError:
        logger.warning("daily_recommendation_cache_get_failed", user_id=str(current_user.id))
        cached = None
    if cached is not None:
        payload = cached.decode() if isinstance(cached, bytes) else cached
        return ResponseModel[TodayRecommendationResponse].model_validate_json(payload)

    repository = SqlAlchemyRecommendationRepository(session)
    had_cache = await repository.get_daily(current_user.id, today) is not None
    use_case = GetTodayRecommendation(repository)
    daily = await use_case.execute(current_user.id, today)
    await SqlAlchemyOperationLogRepository(session).add(
        current_user.id,
        "app_open",
        {"hour_of_day": datetime.now(UTC).hour, "had_cache": had_cache, "scene": scene},
    )
    response = ResponseModel(data=today_response(daily, settings.departure_time, scene))
    try:
        await redis_client.set(
            cache_key,
            response.model_dump_json(),
            ex=_seconds_until_utc_day_end(now),
        )
    except RedisError:
        logger.warning("daily_recommendation_cache_set_failed", user_id=str(current_user.id))
    return response


@router.get("/outfits/{outfit_id}", response_model=ResponseModel[OutfitResponse])
async def get_outfit_detail(
    outfit_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ResponseModel[OutfitResponse]:
    use_case = SqlAlchemyRecommendationRepository(session)
    outfit = await use_case.get_outfit_for_user(outfit_id, current_user.id)
    if outfit is None:
        raise FriendlyException(error_codes.NOT_FOUND, "Outfit not found", status_code=404)
    await SqlAlchemyOperationLogRepository(session).add(
        current_user.id,
        "recommendation_viewed",
        {"outfit_type": outfit.outfit_type},
    )
    return ResponseModel(data=outfit_response(outfit))


@router.post("/outfits/{outfit_id}/wear", response_model=ResponseModel[WearOutfitResponse])
async def wear_outfit(
    outfit_id: uuid.UUID,
    payload: WearOutfitRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ResponseModel[WearOutfitResponse]:
    worn_date = payload.worn_date or datetime.now(UTC).date()
    use_case = WearOutfit(SqlAlchemyRecommendationRepository(session))
    await use_case.execute(outfit_id, current_user.id, worn_date)
    await SqlAlchemyOperationLogRepository(session).add(
        current_user.id,
        "outfit_worn",
        {"outfit_id": str(outfit_id), "worn_date": worn_date.isoformat()},
    )
    return ResponseModel(data=WearOutfitResponse(success=True))


@router.post("/outfits/{outfit_id}/feedback", response_model=ResponseModel[FeedbackResponse])
async def feedback_outfit(
    outfit_id: uuid.UUID,
    payload: FeedbackRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ResponseModel[FeedbackResponse]:
    use_case = FeedbackOutfit(SqlAlchemyRecommendationRepository(session))
    await use_case.execute(outfit_id, current_user.id, payload.feedback_type, payload.reason_tags)
    await SqlAlchemyOperationLogRepository(session).add(
        current_user.id,
        "feedback_given",
        {
            "outfit_id": str(outfit_id),
            "feedback_type": payload.feedback_type,
            "reason_tags": payload.reason_tags,
        },
    )
    return ResponseModel(data=FeedbackResponse(success=True))


@router.get("/outfit-history", response_model=ResponseModel[OutfitHistoryResponse])
async def get_outfit_history(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    start_date: date | None = None,
    end_date: date | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ResponseModel[OutfitHistoryResponse]:
    rows = await SqlAlchemyRecommendationRepository(session).get_outfit_history(
        current_user.id,
        start_date,
        end_date,
        limit,
        offset,
    )
    return ResponseModel(
        data=OutfitHistoryResponse(
            items=[
                OutfitHistoryItemResponse(
                    outfit_id=outfit.id,
                    worn_date=history.worn_date,
                    outfit_type=outfit.outfit_type,
                    title=outfit.title,
                    feedback_type=feedback.feedback_type if feedback else None,
                    reason_tags=(feedback.reason_tags or {}).get("tags", []) if feedback else [],
                )
                for history, outfit, feedback in rows
            ],
            limit=limit,
            offset=offset,
        )
    )
