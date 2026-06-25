"""Wardrobe insights router — M12."""

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.insights import wardrobe_health, wardrobe_insights, wardrobe_report
from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.config.settings import get_settings
from app.infrastructure.persistence.session import get_db_session as get_db
from app.presentation.api.v1.deps import get_current_user
from app.presentation.api.v1.schemas.insights import (
    ColorPaletteResponse,
    CostPerWearItem,
    IdleItemResponse,
    InsightsSummaryResponse,
    StyleTrendResponse,
    TopItemResponse,
    WardrobeHealthResponse,
    WardrobeReportResponse,
)

router = APIRouter(prefix="/v1/insights", tags=["insights"])


@router.get("/summary", response_model=InsightsSummaryResponse)
async def insights_summary(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[Any, Depends(get_current_user)],
) -> InsightsSummaryResponse:
    data = await wardrobe_insights.get_summary(current_user.id, db)
    return InsightsSummaryResponse(**data)


@router.get("/top-items", response_model=list[TopItemResponse])
async def top_items(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[Any, Depends(get_current_user)],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> list[TopItemResponse]:
    items = await wardrobe_insights.get_top_items(current_user.id, db, limit)
    return [
        TopItemResponse(
            item_id=i.id,
            title=i.title,
            category=i.category,
            cover_image_url=i.cover_image_url,
            wear_count=i.wear_count,
        )
        for i in items
    ]


@router.get("/idle-items", response_model=list[IdleItemResponse])
async def idle_items(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[Any, Depends(get_current_user)],
    threshold_days: Annotated[int, Query(ge=1)] = 60,
) -> list[IdleItemResponse]:
    items = await wardrobe_insights.get_idle_items(current_user.id, db, threshold_days)
    return [
        IdleItemResponse(
            item_id=i.id,
            title=i.title,
            category=i.category,
            cover_image_url=i.cover_image_url,
            wear_count=i.wear_count,
        )
        for i in items
    ]


@router.get("/color-palette", response_model=ColorPaletteResponse)
async def color_palette(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[Any, Depends(get_current_user)],
) -> ColorPaletteResponse:
    palette = await wardrobe_insights.get_color_palette(current_user.id, db)
    return ColorPaletteResponse(palette=palette)


@router.get("/style-trend", response_model=StyleTrendResponse)
async def style_trend(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[Any, Depends(get_current_user)],
) -> StyleTrendResponse:
    trend = await wardrobe_insights.get_style_trend(current_user.id, db)
    return StyleTrendResponse(trend=trend)


@router.get("/cost-per-wear", response_model=list[CostPerWearItem])
async def cost_per_wear(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[Any, Depends(get_current_user)],
) -> list[CostPerWearItem]:
    rows = await wardrobe_insights.get_cost_per_wear(current_user.id, db)
    return [CostPerWearItem(**r) for r in rows]


@router.get("/wardrobe-health", response_model=WardrobeHealthResponse)
async def wardrobe_health_score(
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[Any, Depends(get_current_user)],
) -> WardrobeHealthResponse:
    if not get_settings().ENABLE_WARDROBE_HEALTH:
        raise FriendlyException(
            error_codes.FORBIDDEN, "Wardrobe health is not enabled", status_code=503
        )
    result = await wardrobe_health.build_health(current_user.id, db)
    return WardrobeHealthResponse.model_validate(result, from_attributes=True)


@router.get("/reports/{period}", response_model=WardrobeReportResponse)
async def wardrobe_period_report(
    period: str,
    db: Annotated[AsyncSession, Depends(get_db)],
    current_user: Annotated[Any, Depends(get_current_user)],
) -> WardrobeReportResponse:
    """Spotify-Wrapped-style half-year / annual report. ``period`` like ``2026-H1``.

    Off by default; returns 503 when ``ENABLE_WARDROBE_REPORT`` is false. Malformed
    period → 422 (VALIDATION_ERROR) from the aggregation layer.
    """
    if not get_settings().ENABLE_WARDROBE_REPORT:
        raise FriendlyException(
            error_codes.FORBIDDEN, "Wardrobe report is not enabled", status_code=503
        )
    report = await wardrobe_report.build_report(current_user.id, db, period)
    return WardrobeReportResponse.model_validate(report, from_attributes=True)
