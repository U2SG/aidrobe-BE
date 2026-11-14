"""First-outfit gate: cold-start feasibility check.

Answers the FE activation question: does the user have enough wardrobe
to show a first outfit? Returns a structured verdict rather than a boolean
so the client can surface useful onboarding guidance (missing slots) and
honest debug detail (gate_failures).
"""
from __future__ import annotations

import asyncio
import dataclasses
import uuid
from datetime import date
from typing import Literal

from app.application.recommendations.feasibility import has_first_outfit_core, missing_first_outfit_slots
from app.application.recommendations.generator import RecommendationGenerator
from app.common import error_codes
from app.common.enums import RecommendationStatus
from app.common.exceptions import FriendlyException
from app.infrastructure.persistence.models.recommendation import DailyRecommendation, Outfit
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.recommendation_repo import (
    FIRST_OUTFIT_GENERATION_SOURCE,
    SqlAlchemyRecommendationRepository,
)
from app.infrastructure.persistence.repositories.wardrobe_repo import load_wardrobe_image_sources

GateState = Literal["no_outfit", "single_outfit", "ready"]
FIRST_OUTFIT_TIMEOUT_SECONDS = 15
FIRST_OUTFIT_ITEMS_PER_CATEGORY = 24


def missing_core_slots(items: list[WardrobeItem]) -> list[str]:
    """Pure function — exported for unit testing.

    Core completeness: (top AND bottom AND shoes) OR (dress AND shoes).
    Returns the list of missing slots on the *closer* incomplete path.
    Returns [] when at least one path is complete.

    Decision table
    --------------
    - Either path complete           → []
    - Only one path started          → report that path's missing slots
    - Both paths started             → report shorter missing list (separates path on tie)
    - Neither path started (empty)   → report separates path ["top", "bottom", "shoes"]
    """
    return missing_first_outfit_slots(item.category for item in items)


@dataclasses.dataclass
class FirstOutfitGateResult:
    state: GateState
    feasible_outfit_count: int
    missing_slots: list[str]
    first_outfit: Outfit | None
    gate_failures: list[str]
    # True when this call generated today's daily — the caller must then
    # invalidate any cached /today payload (a stale insufficient_items entry
    # would otherwise contradict the gate until end of day).
    generated: bool = False
    daily: DailyRecommendation | None = None


class FirstOutfitGate:
    def __init__(
        self,
        repository: SqlAlchemyRecommendationRepository,
    ) -> None:
        self.repository = repository

    async def execute(self, user_id: uuid.UUID, today: date) -> FirstOutfitGateResult:
        try:
            async with asyncio.timeout(FIRST_OUTFIT_TIMEOUT_SECONDS):
                return await self._evaluate(user_id, today)
        except TimeoutError as exc:
            # The request dependency rolls back any partial write. The recompute
            # already queued by wardrobe updates remains available to the worker.
            raise FriendlyException(
                error_codes.INTERNAL_ERROR, "First outfit generation timed out", status_code=504
            ) from exc

    async def _evaluate(self, user_id: uuid.UUID, today: date) -> FirstOutfitGateResult:
        items = await self.repository.get_wearable_items(user_id)
        missing = missing_core_slots(items)
        if missing:
            return _no_outfit([f"missing_{slot}" for slot in missing], missing_slots=missing)

        items_with_images = await self._items_with_images(items)
        daily = await self.repository.get_daily(user_id, today)
        outfits = _valid_outfits(daily, items_with_images)
        if outfits:
            return _ready(daily, outfits)

        # Only the fallback writer needs the lock. Re-read after waiting so
        # concurrent first-outfit requests and refreshes can supply the result.
        await self.repository.lock_daily_write(user_id, today)
        wardrobe_read_at, items = await self.repository.get_wearable_snapshot(user_id)
        missing = missing_core_slots(items)
        if missing:
            return _no_outfit([f"missing_{slot}" for slot in missing], missing_slots=missing)
        items_with_images = await self._items_with_images(items)
        daily = await self.repository.get_daily(user_id, today)
        outfits = _valid_outfits(daily, items_with_images)
        if outfits:
            return _ready(daily, outfits)
        if not has_first_outfit_core(items_with_images.values()):
            return _no_outfit(["image_unavailable"], daily=daily)

        # Reuse the rule engine with only renderable clothes. Its core pairing
        # search is quadratic in wardrobe depth, so cap the synchronous input
        # per category while preserving the complete core paths.
        per_category: dict[str, int] = {}
        eligible_items: list[WardrobeItem] = []
        for item in sorted(items, key=lambda row: (row.created_at, row.id), reverse=True):
            if item.id not in items_with_images:
                continue
            count = per_category.get(item.category, 0)
            if count < FIRST_OUTFIT_ITEMS_PER_CATEGORY:
                per_category[item.category] = count + 1
                eligible_items.append(item)
        await RecommendationGenerator(self.repository).generate_for_user(
            user_id,
            today,
            generation_source=FIRST_OUTFIT_GENERATION_SOURCE,
            # A repair may supersede an outfit with favorites, feedback or wear.
            # Archiving keeps its identity and references available in history.
            refresh=True,
            wardrobe_snapshot=(wardrobe_read_at, eligible_items),
        )
        daily = await self.repository.get_daily(user_id, today)
        if daily is None or daily.status == RecommendationStatus.FAILED.value:
            raise FriendlyException(
                error_codes.INTERNAL_ERROR, "First outfit generation failed", status_code=500
            )

        # Some clothing may have changed during generation. Validate the result
        # against a fresh wardrobe read before sharing it with the client.
        items = await self.repository.get_wearable_items(user_id)
        items_with_images = await self._items_with_images(items)
        outfits = _valid_outfits(daily, items_with_images)
        if outfits:
            return _ready(daily, outfits, generated=True)
        failures = ["engine_insufficient"] if daily.status == RecommendationStatus.INSUFFICIENT_ITEMS.value else [
            "image_unavailable"
        ]
        return _no_outfit(failures, daily=daily, generated=True)

    async def _items_with_images(self, items: list[WardrobeItem]) -> dict[uuid.UUID, str]:
        sources = await load_wardrobe_image_sources(self.repository.session, items)
        return {item.id: item.category for item in items if any(sources[item.id][0])}


def _valid_outfits(daily: DailyRecommendation | None, items_with_images: dict[uuid.UUID, str]) -> list[Outfit]:
    if daily is None or daily.status != RecommendationStatus.READY.value:
        return []
    return [
        outfit
        for outfit in daily.outfits
        if all(items_with_images.get(item.item_id) == item.role for item in outfit.items)
        and has_first_outfit_core(item.role for item in outfit.items)
    ]


def _no_outfit(
    failures: list[str],
    *,
    missing_slots: list[str] | None = None,
    daily: DailyRecommendation | None = None,
    generated: bool = False,
) -> FirstOutfitGateResult:
    return FirstOutfitGateResult(
        state="no_outfit",
        feasible_outfit_count=0,
        missing_slots=missing_slots or [],
        first_outfit=None,
        gate_failures=failures,
        generated=generated,
        daily=daily,
    )


def _ready(
    daily: DailyRecommendation | None, outfits: list[Outfit], *, generated: bool = False
) -> FirstOutfitGateResult:
    safe_outfits = [outfit for outfit in outfits if outfit.outfit_type == "safe"]
    return FirstOutfitGateResult(
        state="single_outfit" if len(outfits) == 1 else "ready",
        feasible_outfit_count=len(outfits),
        missing_slots=[],
        first_outfit=safe_outfits[0] if safe_outfits else max(outfits, key=lambda outfit: outfit.score_total),
        gate_failures=[],
        generated=generated,
        daily=daily,
    )
