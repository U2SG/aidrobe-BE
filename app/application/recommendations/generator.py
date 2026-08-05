import dataclasses
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import structlog
from sqlalchemy import select

from app.application.outfits.scoring.static import compute_static_breakdown, get_rules_version
from app.application.ports.llm import LlmPort
from app.application.ports.weather import WeatherInfo, WeatherPort
from app.application.preferences.memory import live_negative_values_by_type
from app.application.recommendations.decision_trace import (
    TRACE_SCHEMA_VERSION,
    build_decision_trace_payload,
)
from app.application.recommendations.engine import EngineItem, RecommendationEngine, RelationshipEdge
from app.application.recommendations.feedback_learning import (
    DEFAULT_SUPPRESSION_DAYS,
    derive_learning_signals,
)
from app.application.recommendations.fit_profile import build_fit_profile
from app.application.recommendations.indoor_climate import (
    IndoorClimateSignal,
    infer_indoor_climate,
)
from app.application.style.build_style_profile import build_style_profile
from app.common.enums import (
    OutfitType,
    RecommendationEventType,
    RecommendationStatus,
    TimeSensitivity,
)
from app.common.local_time import local_datetime, utc_now
from app.common.season import current_season
from app.config.settings import get_settings
from app.infrastructure.persistence.models.preference_memory import UserPreferenceMemory
from app.infrastructure.persistence.models.recommendation import (
    DailyRecommendation,
    Outfit,
    OutfitItem,
    RecommendationTrace,
)
from app.infrastructure.persistence.models.user import UserSettings
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.models.wardrobe_relationship import WardrobeItemRelationship
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)
from app.infrastructure.persistence.repositories.style_repo import SqlAlchemyStyleRepository
from app.infrastructure.persistence.repositories.wardrobe_relationship_repo import (
    SqlAlchemyWardrobeRelationshipRepository,
)

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
        context_constraints: list[str] | None = None,
        preserve_refresh_after: datetime | None = None,
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
                    recommendation_id=str(existing.id),
                    revision=existing.revision,
                    trace_id=_trace_id(existing),
                )
                return existing

        items = await self.repository.get_wearable_items(user_id)
        recent = await self.repository.recent_worn_main_item_ids(user_id, target_date)
        settings = get_settings()
        user_settings = await self.repository.session.scalar(
            select(UserSettings).where(UserSettings.user_id == user_id)
        )
        relationship_repository = SqlAlchemyWardrobeRelationshipRepository(self.repository.session)
        if settings.ENABLE_WARDROBE_GRAPH:
            # Rule-based edges are 100% derived from current item metadata (no
            # user data, no rollback concern), so the "persist regardless of
            # flag" convention does not apply — gating keeps the O(n^2) upsert
            # off the hot path when the feature is off.
            await self._persist_rule_based_relationships(user_id, relationship_repository, items)
        relationship_edges = await self._load_relationship_edges(
            user_id,
            relationship_repository,
            items,
            settings,
        )
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
        style_profile = await self._load_style_profile(user_id, target_date)
        weather = await self._load_weather(user_id, target_date)
        # WeatherInfo has no source observation time, so persist when the
        # provider/cache boundary returned the facts used by this generation.
        weather_resolved_at = datetime.now(UTC) if weather is not None else None
        fit_profile = await self._load_fit_profile(user_id)
        # BE#82 — infer indoor-climate tendency (flag-gated; None → legacy ranking).
        indoor_climate = (
            await self._load_indoor_climate(
                user_id,
                user_settings.commute_exposure if user_settings is not None else None,
            )
            if settings.ENABLE_INDOOR_CLIMATE_INFERENCE
            else None
        )
        # BE#46 — let explicit NEGATIVE color preferences down-rank matching
        # outfits. Read only when the flag is on (rows persist regardless), so a
        # flag-off generation is byte-identical to the legacy one.
        disliked_colors = await self._load_disliked_colors(user_id, settings)
        memory_items = await self._load_memory_items(user_id)
        # BE#67 — down-rank looks the user recently dismissed/disliked. Derived at
        # read time from the event stream (no persistence), gated by the flag so a
        # flag-off generation is byte-identical to the legacy one.
        suppressed_outfit_item_ids = await self._load_suppressed_signatures(user_id, settings)

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
                    sub_category=item.sub_category,
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
            disliked_colors=disliked_colors,
            relationship_edges=relationship_edges,
            suppressed_outfit_item_ids=suppressed_outfit_item_ids,
            indoor_climate=indoor_climate,
            context_constraints=context_constraints or [],
        )

        captured_at = datetime.now(UTC)
        context_snapshot = _build_context_snapshot(
            weather,
            scene,
            context_constraints or [],
            captured_at=weather_resolved_at or captured_at,
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
            context_json=context_snapshot,
        )
        pending_daily = daily
        should_write_outfits = True
        if refresh:
            daily = await self.repository.archive_current_and_revise(daily)
        else:
            daily, should_write_outfits = await self.repository.replace_daily(
                daily,
                preserve_refresh_after=preserve_refresh_after,
            )

        if not should_write_outfits:
            logger.info(
                "recommendation_generation_skipped",
                user_id=str(user_id),
                target_date=str(target_date),
                reason="superseded_by_newer_daily",
                recommendation_id=str(daily.id),
                revision=daily.revision,
                trace_id=_trace_id(daily),
            )
            return daily

        existing_trace = None if daily is pending_daily else daily.trace
        trace_id = existing_trace.id if existing_trace is not None else uuid.uuid4()
        persisted_rules_version: str | None = None

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
                    outfit_rules_version = rules_version
                    persisted_rules_version = rules_version
                except Exception:
                    logger.exception(
                        "scoring.static_failed",
                        user_id=str(user_id),
                        outfit_type=engine_outfit.outfit_type,
                        rank=rank,
                        recommendation_id=str(daily.id),
                        revision=daily.revision,
                        trace_id=str(trace_id),
                    )
                    static_breakdown = None
                    outfit_rules_version = None
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
                        score_rules_version=outfit_rules_version,
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

        llm_provider, llm_model = _llm_identity(
            self.engine.llm if result.llm_boundary_used else None
        )
        trace_payload = build_decision_trace_payload(
            target_date=target_date,
            captured_at=captured_at,
            timezone_bucket=(
                user_settings.timezone
                if user_settings is not None and user_settings.timezone
                else settings.DEFAULT_TIMEZONE
            ),
            weather=weather,
            weather_resolved_at=(
                weather_resolved_at.isoformat()
                if weather_resolved_at is not None
                else None
            ),
            commute_exposure=(
                user_settings.commute_exposure if user_settings is not None else None
            ),
            indoor_climate=indoor_climate,
            normalized_constraints=context_constraints or [],
            wardrobe_items=items,
            memory_items=memory_items,
            engine_result=result,
            recent_worn_main_item_ids=recent,
            recent_outfit_item_ids=recent_outfits,
            style_profile=style_profile,
            fit_profile=fit_profile,
            disliked_colors=disliked_colors,
            relationship_edges=relationship_edges,
            suppressed_outfit_item_ids=suppressed_outfit_item_ids,
            avoid_exact_repeats=avoid_exact_repeats,
            persisted_rules_version=persisted_rules_version,
            llm_provider=llm_provider,
            llm_model=llm_model,
            deterministic_seed=None,
        )
        trace = existing_trace or RecommendationTrace(
            id=trace_id,
            daily_recommendation=daily,
            schema_version=TRACE_SCHEMA_VERSION,
            payload=trace_payload,
        )
        trace.schema_version = TRACE_SCHEMA_VERSION
        trace.payload = trace_payload
        await self.repository.save_trace(trace)

        logger.info(
            "recommendation_generated",
            user_id=str(user_id),
            target_date=str(target_date),
            status=result.status,
            has_style_profile=bool(style_profile),
            recommendation_id=str(daily.id),
            revision=daily.revision,
            trace_id=str(trace.id),
        )
        return daily

    async def _persist_rule_based_relationships(
        self,
        user_id: uuid.UUID,
        relationship_repository: SqlAlchemyWardrobeRelationshipRepository,
        items: list[WardrobeItem],
    ) -> None:
        if len(items) < 2:
            return
        try:
            # SAVEPOINT isolation: a DBAPI failure inside relationship work would
            # otherwise leave the shared session in an ABORTED transaction, so
            # every later query/write in generate_for_user raises
            # PendingRollbackError instead of degrading to "no graph data". The
            # nested transaction rolls back ONLY this work and leaves the outer
            # transaction usable. It also makes the wipe+regenerate atomic: a
            # mid-generation failure restores the pre-existing edges rather than
            # leaving them half-deleted.
            async with self.repository.session.begin_nested():
                await relationship_repository.delete_rule_based_for_user(user_id)
                await relationship_repository.generate_rule_based(items)
                # TODO(BE#48): extend this seam with LLM-generated relationship
                # suggestions once they have review and correction controls.
        except Exception as exc:
            logger.warning(
                "wardrobe_relationship_generation_failed",
                user_id=str(user_id),
                error=str(exc),
            )

    async def _load_relationship_edges(
        self,
        user_id: uuid.UUID,
        relationship_repository: SqlAlchemyWardrobeRelationshipRepository,
        items: list[WardrobeItem],
        settings: Any,
    ) -> list[RelationshipEdge]:
        if not settings.ENABLE_WARDROBE_GRAPH:
            return []
        try:
            # SAVEPOINT isolation (same rationale as _persist_rule_based_relationships):
            # a read failure must not leave the shared session in an ABORTED
            # transaction and poison the rest of generate_for_user.
            async with self.repository.session.begin_nested():
                rows_by_id: dict[uuid.UUID, WardrobeItemRelationship] = {}
                for item in items:
                    for row in await relationship_repository.positive_pairs(item.id):
                        rows_by_id[row.id] = row
                    for row in await relationship_repository.rejected_pairs(item.id):
                        rows_by_id[row.id] = row
            return [
                RelationshipEdge(
                    source_item_id=str(row.source_item_id),
                    target_item_id=str(row.target_item_id),
                    relationship_type=row.relationship_type,
                    score=float(row.score),
                    confidence=float(row.confidence),
                )
                for row in rows_by_id.values()
            ]
        except Exception as exc:
            logger.warning(
                "wardrobe_relationship_load_failed",
                user_id=str(user_id),
                error=str(exc),
            )
            return []

    async def _load_disliked_colors(self, user_id: uuid.UUID, settings: Any) -> set[str]:
        """BE#46 — lowercased colors the user has explicitly stated they dislike.

        Off (empty) unless ENABLE_PREFERENCE_MEMORY is on. `color` is the
        discriminating field the engine carries on every item, so it's the hook
        for negative influence; `negative_preference` rows that happen to name a
        color are also honored. Fail-soft: a read error must not abort the daily
        generation."""
        if not settings.ENABLE_PREFERENCE_MEMORY:
            return set()
        try:
            from app.infrastructure.persistence.repositories.preference_memory_repo import (
                SqlAlchemyPreferenceMemoryRepository,
            )

            # Uncapped scoring read: an explicit color dislike must down-rank
            # even when it sits behind the 8-row prompt cap (see review BE#46).
            return await live_negative_values_by_type(
                SqlAlchemyPreferenceMemoryRepository(self.repository.session),
                user_id,
                "color",
                "negative_preference",
            )
        except Exception as exc:
            logger.warning("preference_memory_load_failed", user_id=str(user_id), error=str(exc))
            return set()

    async def _load_suppressed_signatures(
        self, user_id: uuid.UUID, settings: Any
    ) -> list[set[str]]:
        """BE#67 — item-set signatures the user recently dismissed/disliked.

        Off (empty) unless ENABLE_FEEDBACK_LEARNING is on. Derived from the event
        stream and recomputed each generation (no persistence → clean rollback).
        Fail-soft: a read/derivation error must not abort the daily generation."""
        if not settings.ENABLE_FEEDBACK_LEARNING:
            return []
        try:
            now = datetime.now(UTC)
            events = await self.repository.list_recommendation_events(
                user_id, since=now - timedelta(days=DEFAULT_SUPPRESSION_DAYS)
            )
            signals = derive_learning_signals(events, now=now)
            return [set(sig) for sig in signals.suppressed_signatures]
        except Exception as exc:
            logger.warning("feedback_learning_load_failed", user_id=str(user_id), error=str(exc))
            return []

    async def _load_style_profile(
        self,
        user_id: uuid.UUID,
        target_date: date | None = None,
    ) -> dict[str, Any]:
        if self.style_repository is None:
            return {}
        try:
            if target_date is None:
                timezone = await self.repository.get_user_timezone(user_id)
                target_date = local_datetime(
                    utc_now(), timezone, get_settings().DEFAULT_TIMEZONE
                ).date()
            profiles = await self.style_repository.list_by_user(user_id)
            wear_history = await self.repository.get_recent_wear_history(
                user_id,
                target_date,
                days=30,
            )
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

    async def _load_indoor_climate(
        self, user_id: uuid.UUID, commute_exposure: str | None
    ) -> IndoorClimateSignal | None:
        """BE#82 — infer indoor-climate tendency from recent temperature-mismatch
        events + commute exposure. Uses the injected session (not AsyncSessionLocal)
        so it works in-transaction and under tests."""
        events = await self.repository.list_recommendation_events(
            user_id,
            event_type=RecommendationEventType.TEMPERATURE_MISMATCH_REPORTED.value,
            limit=100,
        )
        return infer_indoor_climate(events, commute_exposure, now=datetime.now(UTC))

    async def _load_weather(
        self,
        user_id: uuid.UUID,
        target_date: date | None = None,
    ) -> WeatherInfo | None:
        if self.weather_service is None:
            return None
        location = await self.repository.get_user_location(user_id)
        if not location:
            return None
        try:
            timezone = await self.repository.get_user_timezone(user_id)
            local_today = local_datetime(
                utc_now(), timezone, get_settings().DEFAULT_TIMEZONE
            ).date()
            # BE#77 — a future target date (tomorrow mode) must use the forecast
            # for THAT date; None (no forecast) is an honest degrade — never
            # substitute today's conditions for tomorrow.
            if target_date is not None and target_date > local_today:
                return await self.weather_service.get_forecast(location, target_date)
            # BE#126 — today dresses for the DAY outlook (daytime high + rain
            # peak from the daily forecast, which includes today), not the
            # instantaneous observation: a 03:00 nightly generation would
            # otherwise dress for 03:00 temperatures. Fall back to now-weather
            # when the forecast is unavailable.
            outlook = await self.weather_service.get_forecast(location, local_today)
            if outlook is not None:
                return outlook
            return await self.weather_service.get_weather(location)
        except Exception as exc:
            logger.warning("weather_load_failed", user_id=str(user_id), error=str(exc))
            return None

    async def _load_memory_items(
        self, user_id: uuid.UUID
    ) -> list[UserPreferenceMemory]:
        try:
            from app.infrastructure.persistence.repositories.preference_memory_repo import (
                SqlAlchemyPreferenceMemoryRepository,
            )

            return await SqlAlchemyPreferenceMemoryRepository(
                self.repository.session
            ).list_live(user_id)
        except Exception as exc:
            logger.warning(
                "preference_memory_trace_load_failed",
                user_id=str(user_id),
                error=str(exc),
            )
            return []


def _build_context_snapshot(
    weather: WeatherInfo | None,
    scene: str | None,
    constraints: list[str],
    *,
    captured_at: datetime | None = None,
) -> dict[str, Any] | None:
    """Snapshot the inputs that drove this generation. Stored on the daily
    row so future explainability ("why did we suggest this?") can ground in
    the same weather/scene the engine actually saw, not whatever the user
    sees right now."""
    normalized_constraints = sorted(set(constraints))
    if weather is None and scene is None and not normalized_constraints:
        return None
    snapshot: dict[str, Any] = {
        "fetched_at": (captured_at or datetime.now(UTC)).isoformat()
    }
    if weather is not None:
        snapshot["weather"] = dataclasses.asdict(weather)
    if scene is not None:
        snapshot["scene"] = scene
    if normalized_constraints:
        snapshot["constraints"] = normalized_constraints
    return snapshot


def _time_sensitivity_for_outfit_type(outfit_type: str) -> str:
    if outfit_type == OutfitType.FAST.value:
        return TimeSensitivity.RUSHED.value
    if outfit_type == OutfitType.STYLISH.value:
        return TimeSensitivity.RELAXED.value
    return TimeSensitivity.STANDARD.value


def _trace_id(daily: DailyRecommendation) -> str | None:
    return str(daily.trace.id) if daily.trace is not None else None


def _llm_identity(llm: LlmPort | None) -> tuple[str | None, str | None]:
    if llm is None:
        return None, None
    provider = type(llm).__name__.lower()
    model = getattr(llm, "model", None)
    provider_settings = getattr(llm, "settings", None)
    if not isinstance(model, str) and provider_settings is not None:
        model = getattr(provider_settings, "QWEN_LLM_MODEL", None)
    return provider, model if isinstance(model, str) else None
