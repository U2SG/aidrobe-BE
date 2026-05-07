import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from redis.asyncio import Redis
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.wardrobe.delete_item import DeleteWardrobeItem
from app.application.wardrobe.list_items import ListWardrobeItems
from app.application.wardrobe.update_item import UpdateWardrobeItem
from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.infrastructure.cache.client import (
    get_redis_client,
    invalidate_daily_recommendation_cache,
)
from app.infrastructure.persistence.models.recommendation import Outfit, OutfitItem, WearHistory
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.operation_log_repo import (
    SqlAlchemyOperationLogRepository,
)
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)
from app.infrastructure.persistence.repositories.style_repo import SqlAlchemyStyleRepository
from app.infrastructure.persistence.repositories.wardrobe_repo import SqlAlchemyWardrobeRepository
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.deps import get_current_user
from app.presentation.api.v1.schemas.common import ResponseModel
from app.presentation.api.v1.schemas.wardrobe import (
    DeleteWardrobeItemResponse,
    ItemWearHistoryResponse,
    UpdateWardrobeItemRequest,
    WardrobeItemResponse,
    WardrobeListResponse,
    WearHistoryEntry,
)

router = APIRouter(prefix="/v1/wardrobe-items", tags=["wardrobe"])


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
        last_worn=item.last_worn_at,
        purchase_date=item.purchase_date,
    )


@router.get("", response_model=ResponseModel[WardrobeListResponse])
async def list_wardrobe_items(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    category: str | None = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> ResponseModel[WardrobeListResponse]:
    use_case = ListWardrobeItems(SqlAlchemyWardrobeRepository(session))
    items, total = await use_case.execute(current_user.id, category, limit, offset)
    return ResponseModel(
        data=WardrobeListResponse(
            items=[item_response(item) for item in items],
            limit=limit,
            offset=offset,
            total=total,
        )
    )


@router.get("/{item_id}", response_model=ResponseModel[WardrobeItemResponse])
async def get_wardrobe_item(
    item_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ResponseModel[WardrobeItemResponse]:
    item = await SqlAlchemyWardrobeRepository(session).get_for_user(item_id, current_user.id)
    if item is None:
        raise FriendlyException(error_codes.NOT_FOUND, "Wardrobe item not found", status_code=404)
    return ResponseModel(data=item_response(item))


@router.patch("/{item_id}", response_model=ResponseModel[WardrobeItemResponse])
async def update_wardrobe_item(
    item_id: uuid.UUID,
    payload: UpdateWardrobeItemRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    redis_client: Annotated[Redis, Depends(get_redis_client)],
) -> ResponseModel[WardrobeItemResponse]:
    use_case = UpdateWardrobeItem(
        SqlAlchemyWardrobeRepository(session),
        SqlAlchemyRecommendationRepository(session),
        SqlAlchemyStyleRepository(session),
    )
    item = await use_case.execute(
        item_id,
        current_user.id,
        title=payload.title,
        category=payload.category,
        color_main=payload.color_main,
        status=payload.status,
        location=payload.location,
        purchase_date=payload.purchase_date,
    )
    await invalidate_daily_recommendation_cache(redis_client, current_user.id)
    return ResponseModel(data=item_response(item))


@router.delete("/{item_id}", response_model=ResponseModel[DeleteWardrobeItemResponse])
async def delete_wardrobe_item(
    item_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    redis_client: Annotated[Redis, Depends(get_redis_client)],
) -> ResponseModel[DeleteWardrobeItemResponse]:
    use_case = DeleteWardrobeItem(
        SqlAlchemyWardrobeRepository(session),
        SqlAlchemyRecommendationRepository(session),
        SqlAlchemyStyleRepository(session),
    )
    deleted = await use_case.execute(item_id, current_user.id)
    await SqlAlchemyOperationLogRepository(session).add(
        current_user.id,
        "item_deleted",
        {"item_id": str(item_id)},
    )
    await invalidate_daily_recommendation_cache(redis_client, current_user.id)
    return ResponseModel(data=DeleteWardrobeItemResponse(deleted=deleted))

@router.get(
    "/{item_id}/wear-history",
    response_model=ResponseModel[ItemWearHistoryResponse],
)
async def get_item_wear_history(
    item_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    days: Annotated[int, Query(ge=1, le=365)] = 60,
) -> ResponseModel[ItemWearHistoryResponse]:
    # Verify ownership
    item = await SqlAlchemyWardrobeRepository(session).get_for_user(item_id, current_user.id)
    if item is None:
        raise FriendlyException(error_codes.NOT_FOUND, "Wardrobe item not found", status_code=404)

    cutoff = datetime.now(UTC).date() - timedelta(days=days)
    stmt = (
        select(WearHistory.worn_date, WearHistory.outfit_id, Outfit.outfit_type)
        .join(OutfitItem, OutfitItem.outfit_id == WearHistory.outfit_id)
        .join(Outfit, Outfit.id == WearHistory.outfit_id)
        .where(
            OutfitItem.item_id == item_id,
            WearHistory.user_id == current_user.id,
            WearHistory.worn_date >= cutoff,
        )
        .order_by(WearHistory.worn_date.desc())
    )
    rows = (await session.execute(stmt)).all()

    daily_counts: dict[str, int] = defaultdict(int)
    entries: list[WearHistoryEntry] = []
    for worn_date_val, outfit_id, outfit_type in rows:
        key = worn_date_val.isoformat()
        daily_counts[key] += 1
        entries.append(
            WearHistoryEntry(worn_date=worn_date_val, outfit_id=outfit_id, outfit_type=outfit_type)
        )

    return ResponseModel(
        data=ItemWearHistoryResponse(
            item_id=item_id,
            days=days,
            daily_counts=dict(daily_counts),
            entries=entries,
        )
    )

