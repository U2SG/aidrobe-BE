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

from app.application.recommendations.feasibility import missing_first_outfit_slots
from app.common.enums import RecommendationStatus
from app.infrastructure.persistence.models.recommendation import Outfit
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
        generated = False
        daily = await self.repository.get_daily(user_id, today)
        if daily is None:
            from app.application.recommendations.generator import RecommendationGenerator

            generator = RecommendationGenerator(self.repository)
            await generator.generate_for_user(
                user_id, today, generation_source="first_outfit_gate"
            )
            # Re-fetch so outfits + items are eager-loaded (avoids MissingGreenlet)
            daily = await self.repository.get_daily(user_id, today)
            generated = True

        # Step 3 — derive state from generation result
        if daily is None or daily.status != RecommendationStatus.READY.value:
            return FirstOutfitGateResult(
                state="no_outfit",
                feasible_outfit_count=0,
                missing_slots=[],
                first_outfit=None,
                gate_failures=["engine_insufficient"],
                generated=generated,
            )

        generated_outfits = list(daily.outfits)
        item_map = {item.id: item for item in items}
        missing_cover_ids = {
            item.id for item in items if item.id is not None and not item.cover_image_url
        }
        image_item_ids: set[uuid.UUID] = set()
        if missing_cover_ids:
            image_item_ids = set(
                await self.repository.session.scalars(
                    select(WardrobeItemImage.item_id).where(
                        WardrobeItemImage.item_id.in_(missing_cover_ids)
                    )
                )
            )
        outfits = [
            outfit
            for outfit in generated_outfits
            if all(
                (item := item_map.get(outfit_item.item_id)) is not None
                and bool(item.cover_image_url or item.id in image_item_ids)
                for outfit_item in outfit.items
            )
        ]
        n = len(outfits)

        if n == 0:
            return FirstOutfitGateResult(
                state="no_outfit",
                feasible_outfit_count=0,
                missing_slots=[],
                first_outfit=None,
                gate_failures=[
                    "image_unavailable" if generated_outfits else "engine_insufficient"
                ],
                generated=generated,
            )

        state: GateState = "single_outfit" if n == 1 else "ready"

        # Step 4 — pick first_outfit: safe type preferred, else highest score_total
        safe_outfits = [o for o in outfits if o.outfit_type == "safe"]
        first_outfit: Outfit = (
            safe_outfits[0]
            if safe_outfits
            else max(outfits, key=lambda o: o.score_total)
        )

        return FirstOutfitGateResult(
            state=state,
            feasible_outfit_count=n,
            missing_slots=[],
            first_outfit=first_outfit,
            gate_failures=[],
            generated=generated,
        )
