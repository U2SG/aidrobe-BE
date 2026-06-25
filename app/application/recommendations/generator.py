import dataclasses
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import select

from app.application.outfits.scoring.static import compute_static_breakdown, get_rules_version
from app.application.ports.llm import LlmPort
from app.application.ports.weather import WeatherInfo, WeatherPort
from app.application.recommendations.engine import EngineItem, RecommendationEngine
from app.application.recommendations.fit_profile import build_fit_profile
from app.application.style.build_style_profile import build_style_profile
from app.common.enums import OutfitType, RecommendationStatus, TimeSensitivity
from app.common.season import current_season
from app.config.settings import get_settings
from app.infrastructure.persistence.models.recommendation import (
    DailyRecommendation,
    Outfit,
    OutfitItem,
)
from app.infrastructure.persistence.models.user import UserSettings
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)
from app.infrastructure.persistence.repositories.style_repo import SqlAlchemyStyleRepository

logger = structlog.get_logger(__name__)


class RecommendationGenerator:
    def __init__(
        self,
        repository: SqlAlchemyRecommendationRepository,
        style_repository: SqlAlchemyStyleRepository | None = None,
        engine: RecommendationEngine | None = None,
        llm: "LlmPort | None" = None,
        weather_service: WeatherPort | None = None,
    ) -> None:
        self.repository = repository
        self.style_repository = style_repository
        self.engine = engine or RecommendationEngine(llm=llm)
        self.weather_service = weather_service

    async def generate_for_user(
        self,
        user_id: uuid.UUID,
        target_date: date,
        generation_source: str,
        scene: str | None = None,
        *,
        refresh: bool = False,
    ) -> DailyRecommendation:
        """`refresh=True` archives the current daily as `is_current=False`
        and writes a new revision. Engagement on the old revision (wear /
        feedback) stays attached to those outfits — that is the whole point
        of revisioning. `refresh=False` (default) is the background-recompute
        path: it short-circuits on engagement and overwrites in place."""
        if not refresh:
            existing = await self.repository.get_daily(user_id, target_date)
            if existing and await self.repository.daily_has_engagement(existing.id):
                logger.info(
                    "recommendation_generation_skipped",
                    user_id=str(user_id),
                    target_date=str(target_date),
                    reason="daily_has_engagement",
                )
                return existing

        items = await self.repository.get_wearable_items(user_id)
        recent = await self.repository.recent_worn_main_item_ids(user_id, target_date)
        settings = get_settings()
        avoid_exact_repeats = settings.ENABLE_RECOMMENDATION_DIVERSITY
        # The flag is the SOLE gate for any behavior change: when diversity is
        # off, keep the legacy fixed 7-day worn-similarity window regardless of
        # RECOMMENDATION_DIVERSITY_WINDOW_DAYS, so that setting can't silently
        # alter the always-on repetition penalty.
        window = settings.RECOMMENDATION_DIVERSITY_WINDOW_DAYS if avoid_exact_repeats else 7
        recent_outfits = await self.repository.get_recent_worn_outfit_item_ids(
            user_id, target_date, days=window
        )
        if avoid_exact_repeats:
            # BE#39 — also avoid repeating looks / hero items the user was
            # recently RECOMMENDED (not just ones they wore). Merge into the
            # same inputs the engine's repetition/freshness logic consumes.
            recent_outfits = recent_outfits + await self.repository.get_recent_recommended_outfit_item_ids(
                user_id, target_date, days=window
            )
            recent = recent | await self.repository.recent_recommended_main_item_ids(
                user_id, target_date, days=window
            )
        style_profile = await self._load_style_profile(user_id)
        weather = await self._load_weather(user_id)
        fit_profile = await self._load_fit_profile(user_id)

        result = await self.engine.generate(
            [
                EngineItem(
                    item_id=str(item.id),
                    title=item.title,
                    category=item.category,
                    color_main=item.color_main,
                    location=item.location,
                    fit_status=item.fit_status,
                    fit_intent=item.fit_intent,
                )
                for item in items
            ],
            recent_worn_main_item_ids=recent,
            target_date=target_date,
            style_profile=style_profile,
            weather=weather,
            fit_profile=fit_profile,
            recent_worn_outfit_item_ids=recent_outfits,
            avoid_exact_repeats=avoid_exact_repeats,
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
            context_json=_build_context_snapshot(weather, scene),
        )
        if refresh:
            daily = await self.repository.archive_current_and_revise(daily)
        else:
            daily = await self.repository.replace_daily(daily)

        if result.status == RecommendationStatus.READY.value:
            now = datetime.now(UTC)
            season = current_season(target_date)
            rules_version = get_rules_version()
            item_by_id = {str(item.id): item for item in items}
            for rank, engine_outfit in enumerate(result.outfits, start=1):
                outfit_items = [
                    item_by_id[item_id]
                    for item_id in engine_outfit.item_ids
                    if item_id in item_by_id
                ]
                # Fail-soft: a config typo or surprise item state in scoring
                # must NOT abort the whole daily generation. Persist None →
                # dynamic layer at GET time has its own fallback.
                static_breakdown: dict[str, Any] | None
                try:
                    static_breakdown = compute_static_breakdown(
                        outfit_items,
                        outfit_type=engine_outfit.outfit_type,
                        current_season=season,
                    )
                    persisted_rules_version = rules_version
                except Exception:
                    logger.exception(
                        "scoring.static_failed",
                        user_id=str(user_id),
                        outfit_type=engine_outfit.outfit_type,
                        rank=rank,
                    )
                    static_breakdown = None
                    persisted_rules_version = None
                outfit = await self.repository.add_outfit(
                    Outfit(
                        daily_recommendation_id=daily.id,
                        user_id=user_id,
                        outfit_type=engine_outfit.outfit_type,
                        title=engine_outfit.title,
                        reason_text=engine_outfit.reason_text,
                        score_total=Decimal(str(engine_outfit.score_total)),
                        rank_order=rank,
                        time_sensitivity=_time_sensitivity_for_outfit_type(
                            engine_outfit.outfit_type
                        ),
                        score_breakdown_json=static_breakdown,
                        score_rules_version=persisted_rules_version,
                        reasoning_json=engine_outfit.reasoning or None,
                        created_at=now,
                    )
                )
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
            has_style_profile=bool(style_profile),
        )
        return daily

    async def _load_style_profile(self, user_id: uuid.UUID) -> dict[str, Any]:
        if self.style_repository is None:
            return {}
        try:
            profiles = await self.style_repository.list_by_user(user_id)
            wear_history = await self.repository.get_recent_wear_history(user_id, days=30)
            feedback = await self.repository.get_recent_feedback(user_id, days=30)
            wardrobe_items = await self.repository.get_wearable_items(user_id)
            outfit_main_item_ids = await self.repository.get_outfit_main_item_ids(
                [wh.outfit_id for wh in wear_history]
            )
            return build_style_profile(
                profiles, wear_history, feedback, wardrobe_items, outfit_main_item_ids
            )
        except Exception as exc:
            logger.warning("style_profile_load_failed", user_id=str(user_id), error=str(exc))
            return {}

    async def _load_fit_profile(self, user_id: uuid.UUID) -> dict[str, Any]:
        try:
            from app.infrastructure.persistence.session import AsyncSessionLocal

            async with AsyncSessionLocal() as session:
                settings = await session.scalar(
                    select(UserSettings).where(UserSettings.user_id == user_id)
                )
                if settings is not None and isinstance(settings.preferences_json, dict):
                    fit_profile = settings.preferences_json.get("fit_profile")
                    if isinstance(fit_profile, dict):
                        return fit_profile
                return await build_fit_profile(session, user_id, persist=False)
        except Exception as exc:
            logger.warning("fit_profile_load_failed", user_id=str(user_id), error=str(exc))
            return {}

    async def _load_weather(self, user_id: uuid.UUID) -> WeatherInfo | None:
        if self.weather_service is None:
            return None
        location = await self.repository.get_user_location(user_id)
        if not location:
            return None
        try:
            return await self.weather_service.get_weather(location)
        except Exception as exc:
            logger.warning("weather_load_failed", user_id=str(user_id), error=str(exc))
            return None


def _build_context_snapshot(
    weather: WeatherInfo | None, scene: str | None
) -> dict[str, Any] | None:
    """Snapshot the inputs that drove this generation. Stored on the daily
    row so future explainability ("why did we suggest this?") can ground in
    the same weather/scene the engine actually saw, not whatever the user
    sees right now."""
    if weather is None and scene is None:
        return None
    snapshot: dict[str, Any] = {"fetched_at": datetime.now(UTC).isoformat()}
    if weather is not None:
        snapshot["weather"] = dataclasses.asdict(weather)
    if scene is not None:
        snapshot["scene"] = scene
    return snapshot


def _time_sensitivity_for_outfit_type(outfit_type: str) -> str:
    if outfit_type == OutfitType.FAST.value:
        return TimeSensitivity.RUSHED.value
    if outfit_type == OutfitType.STYLISH.value:
        return TimeSensitivity.RELAXED.value
    return TimeSensitivity.STANDARD.value
