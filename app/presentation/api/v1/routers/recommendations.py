import uuid
from datetime import UTC, date, datetime, time
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Query
from redis.asyncio import Redis
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.outfits.feedback import FeedbackOutfit
from app.application.outfits.obstacles import detect_obstacles, has_dirty
from app.application.outfits.prep import compute_prep
from app.application.outfits.scoring.dynamic import compute_dynamic_scores
from app.application.outfits.wear import WearOutfit
from app.application.recommendations.feedback_tags import get_tags_for_scene
from app.application.recommendations.get_today import GetTodayRecommendation
from app.application.recommendations.style_context import build_style_context
from app.application.wardrobe.submit_fit_feedback import SubmitFitFeedback
from app.common import error_codes
from app.common.category_to_role import map_category_to_role
from app.common.enums import RecommendationStatus, TimeSensitivity
from app.common.exceptions import FriendlyException
from app.common.i18n import I18nMessage
from app.common.season import current_season
from app.config.settings import get_settings
from app.infrastructure.cache.client import get_redis_client
from app.infrastructure.cache.keys import build_daily_recommendation_cache_key
from app.infrastructure.persistence.models.recommendation import DailyRecommendation, Outfit
from app.infrastructure.persistence.models.user import User
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.operation_log_repo import (
    SqlAlchemyOperationLogRepository,
)
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)
from app.infrastructure.persistence.repositories.style_repo import SqlAlchemyStyleRepository
from app.infrastructure.persistence.repositories.user_repo import SqlAlchemyUserRepository
from app.infrastructure.persistence.repositories.wardrobe_repo import SqlAlchemyWardrobeRepository
from app.infrastructure.persistence.session import get_db_session
from app.presentation.api.v1.deps import get_current_user
from app.presentation.api.v1.schemas.common import ResponseModel
from app.presentation.api.v1.schemas.recommendations import (
    FeedbackBundleRequest,
    FeedbackBundleResponse,
    FeedbackRequest,
    FeedbackResponse,
    FeedbackTagResponse,
    FeedbackTagsResponse,
    ObstacleResponse,
    OutfitHistoryItemResponse,
    OutfitHistoryResponse,
    OutfitResponse,
    OutfitScoresResponse,
    RecommendationItemResponse,
    StyleContextResponse,
    TodayContextResponse,
    TodayRecommendationResponse,
    WearOutfitRequest,
    WearOutfitResponse,
    WeatherContextResponse,
)

router = APIRouter(prefix="/v1/recommendations", tags=["recommendations"])
logger = structlog.get_logger(__name__)


def _build_base_outfit_response(outfit: Outfit) -> OutfitResponse:
    return OutfitResponse(
        outfit_id=outfit.id,
        type=outfit.outfit_type,
        title=outfit.title,
        reason=outfit.reason_text,
        time_sensitivity=outfit.time_sensitivity,
        items=[
            RecommendationItemResponse(
                item_id=item.item_id,
                category=item.role,
                display_role=map_category_to_role(item.role),
            )
            for item in sorted(outfit.items, key=lambda i: i.display_order)
        ],
    )


# Backwards-compatible alias for callers that don't need explainability.
def outfit_response(outfit: Outfit) -> OutfitResponse:
    return _build_base_outfit_response(outfit)


async def _enrich_outfits(
    session: AsyncSession, outfits: list[Outfit], today: date
) -> list[OutfitResponse]:
    """Batch-fetch WardrobeItem rows for all outfits, compute dynamic scores /
    obstacles / prep per outfit. Each pipeline gated by its own feature flag —
    when all three flags are off, we don't even hit the DB."""
    settings = get_settings()
    base = [_build_base_outfit_response(o) for o in outfits]
    if not (settings.ENABLE_OUTFIT_SCORES or settings.ENABLE_OBSTACLES or settings.ENABLE_PREP_STEPS):
        return base

    # Single batch query for all wardrobe items referenced across outfits.
    item_ids = {oi.item_id for o in outfits for oi in o.items}
    if not item_ids:
        return base
    rows = (
        await session.execute(select(WardrobeItem).where(WardrobeItem.id.in_(item_ids)))
    ).scalars().all()
    items_by_id: dict[uuid.UUID, WardrobeItem] = {it.id: it for it in rows}
    season = current_season(today)

    enriched: list[OutfitResponse] = []
    for outfit, base_resp in zip(outfits, base, strict=True):
        ordered_outfit_items = sorted(outfit.items, key=lambda i: i.display_order)
        missing_ids = [oi.item_id for oi in ordered_outfit_items if oi.item_id not in items_by_id]
        if missing_ids:
            # Silent drops mask data integrity issues — log but don't fail.
            logger.warning(
                "enrich_outfits.missing_items",
                outfit_id=str(outfit.id),
                missing_ids=[str(mid) for mid in missing_ids],
            )
        ordered_items = [
            items_by_id[oi.item_id]
            for oi in ordered_outfit_items
            if oi.item_id in items_by_id
        ]
        if not ordered_items:
            # All items gone (deleted / orphaned). Return base response without
            # enrichment rather than running helpers on empty input.
            logger.warning("enrich_outfits.empty_after_lookup", outfit_id=str(outfit.id))
            enriched.append(base_resp)
            continue

        # Per-outfit error boundary: one bad outfit must NOT 5xx the whole
        # /today response. On failure we keep the base outfit, drop only the
        # explainability bits.
        try:
            obstacles_payload: list[dict[str, Any]] = []
            if settings.ENABLE_OBSTACLES:
                obstacles_payload = detect_obstacles(
                    ordered_items, today=today, current_season=season
                )
                base_resp.obstacles = [ObstacleResponse(**o) for o in obstacles_payload]
            if settings.ENABLE_OUTFIT_SCORES:
                scores = compute_dynamic_scores(
                    ordered_items,
                    outfit.score_breakdown_json,
                    has_dirty_obstacle=has_dirty(obstacles_payload),
                )
                base_resp.scores = OutfitScoresResponse(**scores.model_dump())
            if settings.ENABLE_PREP_STEPS:
                prep = compute_prep(ordered_items)
                base_resp.prep_steps = [I18nMessage(**m) for m in prep["steps"]]
                base_resp.prep_notes = [I18nMessage(**m) for m in prep["notes"]]
        except Exception:
            logger.exception(
                "enrich_outfits.failed",
                outfit_id=str(outfit.id),
                items_count=len(ordered_items),
            )
            # Reset partial enrichment so client sees a consistent base shape.
            base_resp.obstacles = None
            base_resp.scores = None
            base_resp.prep_steps = None
            base_resp.prep_notes = None
        enriched.append(base_resp)
    return enriched


async def today_response(
    daily: DailyRecommendation,
    session: AsyncSession,
    today: date,
    departure_time: time | None = None,
    scene: str | None = None,
) -> TodayRecommendationResponse:
    guidance = None
    if daily.status == RecommendationStatus.INSUFFICIENT_ITEMS.value:
        guidance = "再加一件下装就能搭"
    outfits = sorted(daily.outfits, key=lambda o: o.rank_order)
    selected_outfits = _select_outfits_for_remaining_time(outfits, departure_time)
    selected_outfits = _select_outfits_for_scene(selected_outfits, scene)
    enriched = (
        await _enrich_outfits(session, selected_outfits, today)
        if daily.id is not None
        else []
    )
    return TodayRecommendationResponse(
        recommendation_date=daily.recommendation_date,
        status=daily.status,
        guidance_text=guidance,
        outfits=enriched,
        context=_build_context_response(daily.context_json),
    )


def _build_context_response(raw: object) -> TodayContextResponse | None:
    """Read context_json from the daily row and surface it only when
    ENABLE_TODAY_CONTEXT is on. The row itself may be persisted regardless
    of the flag — this is a presentation-layer gate so we can roll the
    feature back without losing the data the engine saw."""
    if not get_settings().ENABLE_TODAY_CONTEXT:
        return None
    if not isinstance(raw, dict) or not raw:
        return None
    weather_raw = raw.get("weather")
    weather: WeatherContextResponse | None = None
    if isinstance(weather_raw, dict):
        try:
            weather = WeatherContextResponse(**weather_raw)
        except Exception:
            # Persisted snapshot pre-dates the current schema. Don't 500 the
            # request — drop the malformed weather chunk and keep the rest.
            logger.warning("today_context.weather_decode_failed")
            weather = None
    scene_raw = raw.get("scene")
    fetched_at_raw = raw.get("fetched_at")
    return TodayContextResponse(
        weather=weather,
        scene=scene_raw if isinstance(scene_raw, str) else None,
        fetched_at=fetched_at_raw if isinstance(fetched_at_raw, str) else None,
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
    style_ctx_data = await build_style_context(current_user.id, session)
    style_ctx = StyleContextResponse(**style_ctx_data)
    today_data = await today_response(daily, session, today, settings.departure_time, scene)
    today_data.style_context = style_ctx
    response: ResponseModel[TodayRecommendationResponse] = ResponseModel(data=today_data)
    try:
        await redis_client.set(
            cache_key,
            response.model_dump_json(),
            ex=_seconds_until_utc_day_end(now),
        )
    except RedisError:
        logger.warning("daily_recommendation_cache_set_failed", user_id=str(current_user.id))
    return response


@router.post("/today/refresh", response_model=ResponseModel[TodayRecommendationResponse])
async def refresh_today(
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    redis_client: Annotated[Redis, Depends(get_redis_client)],
    scene: str | None = Query(default=None, pattern="^(commute|formal|casual)$"),
) -> ResponseModel[TodayRecommendationResponse]:
    """Phase D3 — user-initiated refresh. Writes a new revision; old revision
    rows + their wear/feedback references are preserved. Rate-limited per
    `TODAY_REFRESH_DAILY_LIMIT` (default 3/d incl. background rev-1)."""
    from app.application.recommendations.refresh_today import refresh_today_recommendation

    repository = SqlAlchemyRecommendationRepository(session)
    style_repo = SqlAlchemyStyleRepository(session)
    daily = await refresh_today_recommendation(
        current_user.id, repository, style_repo, scene=scene
    )
    await session.commit()

    # Drop any stale /today cache for this user so the next GET reads the
    # new revision rather than serving the prior cached payload.
    user_settings = await SqlAlchemyUserRepository(session).get_or_create_settings(
        current_user.id
    )
    cache_key = build_daily_recommendation_cache_key(
        current_user.id, daily.recommendation_date, user_settings.departure_time, scene
    )
    try:
        await redis_client.delete(cache_key)
    except RedisError:
        logger.warning("today_refresh_cache_invalidate_failed", user_id=str(current_user.id))

    today_data = await today_response(
        daily, session, daily.recommendation_date, user_settings.departure_time, scene
    )
    return ResponseModel(data=today_data)


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
    enriched = await _enrich_outfits(session, [outfit], datetime.now(UTC).date())
    return ResponseModel(data=enriched[0])


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


@router.post("/outfits/{outfit_id}/feedback/bundle", response_model=ResponseModel[FeedbackBundleResponse])
async def feedback_outfit_bundle(
    outfit_id: uuid.UUID,
    payload: FeedbackBundleRequest,
    current_user: Annotated[User, Depends(get_current_user)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> ResponseModel[FeedbackBundleResponse]:
    # Validate ownership + item-to-outfit scope BEFORE any write so we never
    # contaminate fit_feedback with cross-outfit item_ids.
    rec_repo = SqlAlchemyRecommendationRepository(session)
    outfit = await rec_repo.get_outfit_for_user(outfit_id, current_user.id)
    if outfit is None:
        raise FriendlyException(error_codes.NOT_FOUND, "Outfit not found", status_code=404)
    outfit_item_ids = {oi.item_id for oi in outfit.items}
    for fit_item in payload.items:
        if fit_item.item_id not in outfit_item_ids:
            raise FriendlyException(
                error_codes.VALIDATION_ERROR,
                "Item does not belong to this outfit",
                status_code=422,
            )

    async with session.begin_nested():
        feedback_uc = FeedbackOutfit(rec_repo)
        await feedback_uc.execute(
            outfit_id, current_user.id, payload.overall.feedback_type, payload.overall.reason_tags
        )
        wardrobe_repo = SqlAlchemyWardrobeRepository(session)
        fit_uc = SubmitFitFeedback(wardrobe_repo, session)
        for fit_item in payload.items:
            scene_val = fit_item.scene.value if fit_item.scene is not None else "casual"
            await fit_uc.execute(current_user.id, fit_item.item_id, outfit_id, fit_item.feedback_type.value, scene_val)
    written = 1 + len(payload.items)
    await SqlAlchemyOperationLogRepository(session).add(
        current_user.id,
        "feedback_bundle_given",
        {"outfit_id": str(outfit_id), "feedback_type": payload.overall.feedback_type, "item_count": len(payload.items)},
    )
    return ResponseModel(data=FeedbackBundleResponse(written=written))


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


@router.get("/feedback-tags", response_model=ResponseModel[FeedbackTagsResponse])
async def get_feedback_tags(
    current_user: Annotated[User, Depends(get_current_user)],
    scene: str | None = Query(default=None, max_length=32),
) -> ResponseModel[FeedbackTagsResponse]:
    tags = get_tags_for_scene(scene)
    return ResponseModel(
        data=FeedbackTagsResponse(
            scene=scene,
            tags=[
                FeedbackTagResponse(
                    key=t.key, label_zh_cn=t.label_zh_cn, applies_to=t.applies_to
                )
                for t in tags
            ],
        )
    )


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
