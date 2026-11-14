import uuid
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from typing import Annotated, cast

from fastapi import APIRouter, Depends, Query
from redis.asyncio import Redis
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.wardrobe.delete_item import DeleteWardrobeItem
from app.application.wardrobe.list_items import ListWardrobeItems
from app.application.wardrobe.update_item import UpdateWardrobeItem
from app.common import error_codes
from app.common.enums import CutoutStatus, RecommendationEventType, WardrobeItemStatus
from app.common.exceptions import FriendlyException
from app.common.item_labels import localized_item_name
from app.common.local_time import local_datetime, utc_now
from app.common.season import SeasonName
from app.config.settings import get_settings
from app.infrastructure.cache.client import (
    get_redis_client,
    invalidate_daily_recommendation_cache,
)
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.recommendation import (
    Outfit,
    OutfitItem,
    RecommendationEvent,
    WearHistory,
)
from app.infrastructure.persistence.models.user import User, UserSettings
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
from app.presentation.api.v1.deps import get_current_user, resolve_user_locale
from app.presentation.api.v1.render_url import WardrobeImageUrls, wardrobe_image_urls
from app.presentation.api.v1.schemas.common import ResponseModel
from app.presentation.api.v1.schemas.wardrobe import (
    DeleteWardrobeItemResponse,
    ItemWearHistoryResponse,
    MarkItemWornRequest,
    UpdateWardrobeItemRequest,
    WardrobeItemResponse,
    WardrobeListResponse,
    WearHistoryEntry,
    season_tag_list,
)

router = APIRouter(prefix="/v1/wardrobe-items", tags=["wardrobe"])


async def _rejected_product_images(
    session: AsyncSession, user_id: uuid.UUID, items: list[WardrobeItem]
) -> set[uuid.UUID]:
    legacy_ids = [item.id for item in items if item.cutout_status == CutoutStatus.SKIPPED.value]
    if not legacy_ids:
        return set()
    rejected = await session.scalars(
        select(AsyncJob.target_id).where(
            AsyncJob.user_id == user_id,
            AsyncJob.job_type == "cutout",
            AsyncJob.target_id.in_(legacy_ids),
            AsyncJob.result["review"].as_string() == "rejected",
        )
    )
    return {item_id for item_id in rejected if item_id is not None}


def item_response(
    item: WardrobeItem, locale: str, images: WardrobeImageUrls, *, rejected: bool = False
) -> WardrobeItemResponse:
    localized_names = cast(dict[str, object] | None, item.localized_names)
    return WardrobeItemResponse(
        item_id=item.id,
        title=item.title,
        display_name=localized_item_name(
            title=item.title,
            color_main=item.color_main,
            category=item.category,
            locale=locale,
            localized_names=localized_names,
            name_source=item.name_source,
        ),
        canonical_name=item.canonical_name,
        localized_names=cast(dict[str, str] | None, item.localized_names),
        name_source=item.name_source,
        category=item.category,
        color_main=item.color_main,
        status=item.status,
        cover_image_url=images.cover,
        original_image_url=images.original,
        cutout_status=CutoutStatus.REJECTED.value if rejected else item.cutout_status,
        location=item.location,
        size_label=item.size_label,
        size_region=item.size_region,
        fit_intent=item.fit_intent,
        fit_status=item.fit_status,
        comfort_score=item.comfort_score,
        sub_category=item.sub_category,
        color_secondary=item.color_secondary,
        time_cost_level=item.time_cost_level,
        purchase_price=float(item.purchase_price) if item.purchase_price is not None else None,
        material=item.material,
        season_tags=season_tag_list(item.season_tags),
        style_tags=item.style_tags if isinstance(item.style_tags, list) else None,
        formality_score=item.formality_score,
        wear_count=item.wear_count,
        last_worn=item.last_worn_at,
        purchase_date=item.purchase_date,
    )


@router.get("", response_model=ResponseModel[WardrobeListResponse])
async def list_wardrobe_items(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    category: str | None = None,
    # Enum/Literal-typed so an invalid value 422s early instead of silently
    # returning an empty list.
    status: WardrobeItemStatus | None = None,
    season: SeasonName | None = None,
    color: Annotated[str | None, Query(max_length=32)] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
    offset: Annotated[int, Query(ge=0)] = 0,
    locale: Annotated[str | None, Query(max_length=16)] = None,
) -> ResponseModel[WardrobeListResponse]:
    use_case = ListWardrobeItems(SqlAlchemyWardrobeRepository(session))
    items, total = await use_case.execute(
        current_user.id, category, limit, offset,
        status=status.value if status else None, season=season, color=color,
    )
    effective_locale = await resolve_user_locale(session, current_user.id, locale)
    images = await wardrobe_image_urls(session, items)
    rejected = await _rejected_product_images(session, current_user.id, items)
    return ResponseModel(
        data=WardrobeListResponse(
            items=[
                item_response(item, effective_locale, images[item.id], rejected=item.id in rejected)
                for item in items
            ],
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
    locale: Annotated[str | None, Query(max_length=16)] = None,
) -> ResponseModel[WardrobeItemResponse]:
    item = await SqlAlchemyWardrobeRepository(session).get_for_user(item_id, current_user.id)
    if item is None:
        raise FriendlyException(error_codes.NOT_FOUND, "Wardrobe item not found", status_code=404)
    effective_locale = await resolve_user_locale(session, current_user.id, locale)
    images = await wardrobe_image_urls(session, [item])
    rejected = await _rejected_product_images(session, current_user.id, [item])
    return ResponseModel(data=item_response(item, effective_locale, images[item.id], rejected=item.id in rejected))


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
        attributes=payload.model_dump(
            include={
                "sub_category",
                "color_secondary",
                "material",
                "season_tags",
                "style_tags",
                "formality_score",
                "time_cost_level",
                "purchase_price",
            }
        ),
    )
    await invalidate_daily_recommendation_cache(redis_client, current_user.id)
    effective_locale = await resolve_user_locale(session, current_user.id)
    images = await wardrobe_image_urls(session, [item])
    return ResponseModel(data=item_response(item, effective_locale, images[item.id]))


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

    user_settings = await session.scalar(
        select(UserSettings).where(UserSettings.user_id == current_user.id)
    )
    cutoff = local_datetime(
        utc_now(),
        user_settings.timezone if user_settings else None,
        get_settings().DEFAULT_TIMEZONE,
    ).date() - timedelta(days=days)
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

    # Standalone manual wears (POST /{item_id}/wears) have no WearHistory
    # row — they live as RecommendationEvents with worn_date in metadata.
    # Include them so this surface stays consistent with wear_count.
    manual_rows = (
        await session.scalars(
            select(RecommendationEvent.event_metadata["worn_date"].astext)
            .where(
                RecommendationEvent.user_id == current_user.id,
                RecommendationEvent.item_id == item_id,
                RecommendationEvent.event_type == RecommendationEventType.WORN.value,
                RecommendationEvent.source == "manual",
                RecommendationEvent.event_metadata["worn_date"].astext >= cutoff.isoformat(),
            )
        )
    ).all()

    daily_counts: dict[str, int] = defaultdict(int)
    entries: list[WearHistoryEntry] = []
    for worn_date_val, outfit_id, outfit_type in rows:
        key = worn_date_val.isoformat()
        daily_counts[key] += 1
        entries.append(WearHistoryEntry(worn_date=worn_date_val, outfit_id=outfit_id, outfit_type=outfit_type))
    for worn_date_str in manual_rows:
        daily_counts[worn_date_str] += 1
        entries.append(WearHistoryEntry(worn_date=date.fromisoformat(worn_date_str), source="manual"))
    entries.sort(key=lambda e: e.worn_date, reverse=True)

    return ResponseModel(
        data=ItemWearHistoryResponse(
            item_id=item_id,
            days=days,
            daily_counts=dict(daily_counts),
            entries=entries,
        )
    )


@router.post(
    "/{item_id}/wears",
    response_model=ResponseModel[WardrobeItemResponse],
)
async def mark_item_worn(
    item_id: uuid.UUID,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    redis_client: Annotated[Redis, Depends(get_redis_client)],
    payload: MarkItemWornRequest | None = None,
) -> ResponseModel[WardrobeItemResponse]:
    """Record that the user wore this item (no outfit context), for
    `worn_date` (defaults to the user's local date).

    Updates last_worn_at and wear_count on the item so insights / detail
    screens reflect the real-world wear immediately. Also emits a
    RecommendationEvent for future preference learning.
    """
    item = await SqlAlchemyWardrobeRepository(session).get_for_user(item_id, current_user.id)
    if item is None:
        raise FriendlyException(error_codes.NOT_FOUND, "Wardrobe item not found", status_code=404)

    user_settings = await session.scalar(
        select(UserSettings).where(UserSettings.user_id == current_user.id)
    )
    worn_date = (payload.worn_date if payload else None) or local_datetime(
        utc_now(),
        user_settings.timezone if user_settings else None,
        get_settings().DEFAULT_TIMEZONE,
    ).date()
    effective_locale = await resolve_user_locale(session, current_user.id)

    # Idempotent per (item, worn_date), mirroring the outfit wear path — a
    # double-tap must not double-count.
    duplicate = await session.scalar(
        select(RecommendationEvent.id).where(
            RecommendationEvent.user_id == current_user.id,
            RecommendationEvent.item_id == item_id,
            RecommendationEvent.event_type == RecommendationEventType.WORN.value,
            RecommendationEvent.source == "manual",
            RecommendationEvent.event_metadata["worn_date"].astext == worn_date.isoformat(),
        )
    )
    if duplicate is not None:
        images = await wardrobe_image_urls(session, [item])
        return ResponseModel(data=item_response(item, effective_locale, images[item.id]))

    session.add(
        RecommendationEvent(
            user_id=current_user.id,
            item_id=item_id,
            event_type=RecommendationEventType.WORN.value,
            source="manual",
            event_metadata={"worn_date": worn_date.isoformat()},
            created_at=datetime.now(UTC),
        )
    )
    # Atomic in SQL (same pattern as the outfit wear path): concurrent wears
    # must not lose increments, and last_worn_at only moves forward.
    await session.execute(
        update(WardrobeItem)
        .where(WardrobeItem.id == item.id)
        .values(
            wear_count=WardrobeItem.wear_count + 1,
            last_worn_at=func.greatest(
                func.coalesce(WardrobeItem.last_worn_at, worn_date),
                worn_date,
            ),
        )
    )
    await session.flush()
    await session.refresh(item)
    await invalidate_daily_recommendation_cache(redis_client, current_user.id)
    images = await wardrobe_image_urls(session, [item])
    return ResponseModel(data=item_response(item, effective_locale, images[item.id]))
