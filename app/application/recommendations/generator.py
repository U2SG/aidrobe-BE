import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

import structlog

from app.application.recommendations.engine import EngineItem, RecommendationEngine
from app.common.enums import RecommendationStatus
from app.infrastructure.persistence.models.recommendation import (
    DailyRecommendation,
    Outfit,
    OutfitItem,
)
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)

logger = structlog.get_logger(__name__)


class RecommendationGenerator:
    def __init__(
        self,
        repository: SqlAlchemyRecommendationRepository,
        engine: RecommendationEngine | None = None,
    ) -> None:
        self.repository = repository
        self.engine = engine or RecommendationEngine()

    async def generate_for_user(
        self, user_id: uuid.UUID, target_date: date, generation_source: str
    ) -> DailyRecommendation:
        items = await self.repository.get_wearable_items(user_id)
        recent = await self.repository.recent_worn_main_item_ids(user_id, target_date)
        result = self.engine.generate(
            [
                EngineItem(
                    item_id=str(item.id),
                    title=item.title,
                    category=item.category,
                    color_main=item.color_main,
                )
                for item in items
            ],
            recent_worn_main_item_ids=recent,
            target_date=target_date,
        )

        daily = DailyRecommendation(
            user_id=user_id,
            recommendation_date=target_date,
            status=result.status,
            generation_source=generation_source,
            score_breakdown={
                outfit.outfit_type: outfit.score_breakdown for outfit in result.outfits
            }
            if result.outfits
            else None,
        )
        daily = await self.repository.replace_daily(daily)

        if result.status == RecommendationStatus.READY.value:
            now = datetime.now(UTC)
            for rank, engine_outfit in enumerate(result.outfits, start=1):
                outfit = await self.repository.add_outfit(
                    Outfit(
                        daily_recommendation_id=daily.id,
                        user_id=user_id,
                        outfit_type=engine_outfit.outfit_type,
                        title=engine_outfit.title,
                        reason_text=engine_outfit.reason_text,
                        score_total=Decimal(str(engine_outfit.score_total)),
                        rank_order=rank,
                        created_at=now,
                    )
                )
                item_by_id = {str(item.id): item for item in items}
                for order, item_id in enumerate(engine_outfit.item_ids, start=1):
                    item = item_by_id[item_id]
                    await self.repository.add_outfit_item(
                        OutfitItem(
                            outfit_id=outfit.id,
                            item_id=item.id,
                            role=item.category,
                            display_order=order,
                            created_at=now,
                        )
                    )

        logger.info(
            "recommendation_generated",
            user_id=str(user_id),
            target_date=str(target_date),
            status=result.status,
        )
        return daily
