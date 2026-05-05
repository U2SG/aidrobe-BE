"""Wardrobe insights router — M12."""

from typing import Any

from fastapi import APIRouter, Depends, Query
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.insights import wardrobe_insights
from app.presentation.api.v1.deps import get_current_user
from app.infrastructure.persistence.session import get_db_session as get_db
from app.presentation.api.v1.schemas.insights import (
    ColorPaletteResponse,
    CostPerWearItem,
    IdleItemResponse,
    InsightsSummaryResponse,
    StyleTrendResponse,
    TopItemResponse,
)

router = APIRouter(prefix="/v1/insights", tags=["insights"])


@router.get("/summary", response_model=InsightsSummaryResponse)
async def insights_summary(
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
) -> InsightsSummaryResponse:
    data = await wardrobe_insights.get_summary(current_user.id, db)
    return InsightsSummaryResponse(**data)


@router.get("/top-items", response_model=list[TopItemResponse])
async def top_items(
    limit: int = Query(10, ge=1, le=50),
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
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
    threshold_days: int = Query(60, ge=1),
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
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
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
) -> ColorPaletteResponse:
    palette = await wardrobe_insights.get_color_palette(current_user.id, db)
    return ColorPaletteResponse(palette=palette)


@router.get("/style-trend", response_model=StyleTrendResponse)
async def style_trend(
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
) -> StyleTrendResponse:
    trend = await wardrobe_insights.get_style_trend(current_user.id, db)
    return StyleTrendResponse(trend=trend)


@router.get("/cost-per-wear", response_model=list[CostPerWearItem])
async def cost_per_wear(
    db: AsyncSession = Depends(get_db),
    current_user: Any = Depends(get_current_user),
) -> list[CostPerWearItem]:
    rows = await wardrobe_insights.get_cost_per_wear(current_user.id, db)
    return [CostPerWearItem(**r) for r in rows]
