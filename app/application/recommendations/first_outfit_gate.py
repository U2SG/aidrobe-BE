"""First-outfit gate: cold-start feasibility check.

Answers the FE activation question: does the user have enough wardrobe
to show a first outfit? Returns a structured verdict rather than a boolean
so the client can surface useful onboarding guidance (missing slots) and
honest debug detail (gate_failures).
"""
from __future__ import annotations

import dataclasses
import uuid
from datetime import date
from typing import Literal

from sqlalchemy import select

from app.common.enums import RecommendationStatus
from app.infrastructure.persistence.models.recommendation import DailyRecommendation, Outfit
from app.infrastructure.persistence.models.wardrobe import WardrobeItem, WardrobeItemImage
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)


GateState = Literal["no_outfit", "single_outfit", "ready"]


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
    cats = {item.category for item in items}

    has_top = "top" in cats
    has_bottom = "bottom" in cats
    has_dress = "dress" in cats
    has_shoes = "shoes" in cats

    # Separates path: top + bottom + shoes
    sep_missing = []
    if not has_top:
        sep_missing.append("top")
    if not has_bottom:
        sep_missing.append("bottom")
    if not has_shoes:
        sep_missing.append("shoes")

    # Dress path: dress + shoes
    dress_missing = []
    if not has_dress:
        dress_missing.append("dress")
    if not has_shoes:
        dress_missing.append("shoes")

    # Both complete
    if not sep_missing:
        return []
    if not dress_missing:
        return []

    # Determine which path is "started" (has at least one slot present)
    sep_started = has_top or has_bottom or has_shoes
    dress_started = has_dress or has_shoes

    if sep_started and not dress_started:
        return sep_missing
    if dress_started and not sep_started:
        return dress_missing
    if sep_started and dress_started:
        # Both started — report the one with strictly fewer missing.
        # On a tie, prefer separates path (spec: "equally far → separates path").
        if len(dress_missing) < len(sep_missing):
            return dress_missing
        return sep_missing
    # Neither started (empty wardrobe)
    return sep_missing  # separates path is the default


@dataclasses.dataclass
class FirstOutfitGateResult:
    state: GateState
    feasible_outfit_count: int
    missing_slots: list[str]
    first_outfit: Outfit | None
    gate_failures: list[str]


class FirstOutfitGate:
    def __init__(
        self,
        repository: SqlAlchemyRecommendationRepository,
    ) -> None:
        self.repository = repository

    async def execute(self, user_id: uuid.UUID, today: date) -> FirstOutfitGateResult:
        """Check wardrobe feasibility and return a structured gate verdict."""
        items = await self.repository.get_wearable_items(user_id)

        # Step 1 — slot check
        missing = missing_core_slots(items)
        if missing:
            return FirstOutfitGateResult(
                state="no_outfit",
                feasible_outfit_count=0,
                missing_slots=missing,
                first_outfit=None,
                gate_failures=[f"missing_{slot}" for slot in missing],
            )

        # Step 2 — get-or-generate today's daily (mirror refresh_today's re-fetch pattern)
        daily = await self.repository.get_daily(user_id, today)
        if daily is None:
            from app.application.recommendations.generator import RecommendationGenerator

            generator = RecommendationGenerator(self.repository)
            await generator.generate_for_user(
                user_id, today, generation_source="first_outfit_gate"
            )
            # Re-fetch so outfits + items are eager-loaded (avoids MissingGreenlet)
            daily = await self.repository.get_daily(user_id, today)

        # Step 3 — derive state from generation result
        if daily is None or daily.status != RecommendationStatus.READY.value:
            return FirstOutfitGateResult(
                state="no_outfit",
                feasible_outfit_count=0,
                missing_slots=[],
                first_outfit=None,
                gate_failures=["engine_insufficient"],
            )

        outfits = list(daily.outfits)
        n = len(outfits)

        if n == 0:
            return FirstOutfitGateResult(
                state="no_outfit",
                feasible_outfit_count=0,
                missing_slots=[],
                first_outfit=None,
                gate_failures=["engine_insufficient"],
            )

        state: GateState = "single_outfit" if n == 1 else "ready"

        # Step 4 — pick first_outfit: safe type preferred, else highest score_total
        safe_outfits = [o for o in outfits if o.outfit_type == "safe"]
        first_outfit: Outfit = (
            safe_outfits[0]
            if safe_outfits
            else max(outfits, key=lambda o: o.score_total)
        )

        # Step 5 — image_unavailable check (non-blocking; FE has SVG fallback).
        # We check cover_image_url from the in-memory items list (safe: column
        # attribute), then fall back to a targeted DB query for items rows only
        # when a cover is missing (avoids lazy-loading the images relationship
        # in async context, which would raise MissingGreenlet).
        gate_failures: list[str] = []
        item_map = {it.id: it for it in items}
        outfit_item_ids = [oi.item_id for oi in first_outfit.items]
        for item_id in outfit_item_ids:
            wardrobe_item = item_map.get(item_id)
            if wardrobe_item is not None and not wardrobe_item.cover_image_url:
                # cover absent — check for any image row via explicit query
                image_count = await self.repository.session.scalar(
                    select(WardrobeItemImage).where(
                        WardrobeItemImage.item_id == item_id
                    ).limit(1)
                )
                if image_count is None:
                    gate_failures.append("image_unavailable")
                    break

        return FirstOutfitGateResult(
            state=state,
            feasible_outfit_count=n,
            missing_slots=[],
            first_outfit=first_outfit,
            gate_failures=gate_failures,
        )
