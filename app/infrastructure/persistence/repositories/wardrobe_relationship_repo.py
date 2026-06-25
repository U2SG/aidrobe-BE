import uuid
from itertools import combinations
from typing import Any, cast

from sqlalchemy import delete, func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.recommendations.colors import COLOR_CONFLICTS, NEUTRAL_COLORS
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.models.wardrobe_relationship import (
    POSITIVE_RELATIONSHIP_TYPES,
    REJECTED_RELATIONSHIP_TYPES,
    WardrobeItemRelationship,
)


class SqlAlchemyWardrobeRelationshipRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def positive_pairs(
        self,
        item_id: uuid.UUID,
        *,
        threshold: float = 0.3,
    ) -> list[WardrobeItemRelationship]:
        result = await self.session.scalars(
            select(WardrobeItemRelationship)
            .where(
                or_(
                    WardrobeItemRelationship.source_item_id == item_id,
                    WardrobeItemRelationship.target_item_id == item_id,
                ),
                WardrobeItemRelationship.relationship_type.in_(POSITIVE_RELATIONSHIP_TYPES),
                WardrobeItemRelationship.score >= threshold,
            )
            .order_by(WardrobeItemRelationship.score.desc())
        )
        return list(result)

    async def rejected_pairs(self, item_id: uuid.UUID) -> list[WardrobeItemRelationship]:
        result = await self.session.scalars(
            select(WardrobeItemRelationship)
            .where(
                or_(
                    WardrobeItemRelationship.source_item_id == item_id,
                    WardrobeItemRelationship.target_item_id == item_id,
                ),
                or_(
                    WardrobeItemRelationship.relationship_type.in_(REJECTED_RELATIONSHIP_TYPES),
                    WardrobeItemRelationship.score < -0.3,
                ),
            )
            .order_by(WardrobeItemRelationship.score.asc())
        )
        return list(result)

    async def upsert_relationship(
        self,
        *,
        source_item_id: uuid.UUID,
        target_item_id: uuid.UUID,
        relationship_type: str,
        score: float,
        confidence: float,
        source: str,
        metadata_json: dict[str, Any] | None = None,
    ) -> WardrobeItemRelationship:
        # Canonicalize the pair here so no caller can bypass ordering: the engine
        # keys edges by frozenset, so an unordered (B, A, type) row alongside
        # (A, B, type) would double-count the boost/penalty.
        source_item_id, target_item_id = _ordered_pair(source_item_id, target_item_id)
        insert_stmt = pg_insert(WardrobeItemRelationship).values(
            source_item_id=source_item_id,
            target_item_id=target_item_id,
            relationship_type=relationship_type,
            score=_clamp(score, -1.0, 1.0),
            confidence=_clamp(confidence, 0.0, 1.0),
            source=source,
            metadata_json=metadata_json or {},
        )
        stmt = (
            insert_stmt.on_conflict_do_update(
                index_elements=["source_item_id", "target_item_id", "relationship_type"],
                set_={
                    "score": insert_stmt.excluded.score,
                    "confidence": insert_stmt.excluded.confidence,
                    "metadata_json": insert_stmt.excluded.metadata_json,
                    "updated_at": func.now(),
                },
            )
            .returning(WardrobeItemRelationship.id)
        )
        row_id = (await self.session.execute(stmt)).scalar_one()
        row = await self.session.get(
            WardrobeItemRelationship,
            row_id,
            populate_existing=True,
        )
        assert row is not None
        return row

    async def delete_rule_based_for_user(self, user_id: uuid.UUID) -> None:
        # Rule-based edges are fully re-derived from current item metadata on
        # every regeneration. When a rule re-derives a DIFFERENT type for the
        # same pair (e.g. a color edit turns a style_clash into a neutral), the
        # upsert conflict target (source, target, type) inserts a NEW row and
        # the stale one survives, corrupting ranking. Wiping the user's
        # rule_based rows before re-inserting keeps the set authoritative.
        #
        # Scope by item ownership (the table has no user_id) via a subquery, and
        # match either endpoint so an edge survives even after one of its items
        # drops out of the wearable set. The source == "rule_based" filter is
        # load-bearing: it must never touch user_feedback / llm edges.
        owned_item_ids = select(WardrobeItem.id).where(WardrobeItem.user_id == user_id)
        await self.session.execute(
            delete(WardrobeItemRelationship).where(
                WardrobeItemRelationship.source == "rule_based",
                or_(
                    WardrobeItemRelationship.source_item_id.in_(owned_item_ids),
                    WardrobeItemRelationship.target_item_id.in_(owned_item_ids),
                ),
            )
        )

    async def generate_rule_based(
        self,
        items: list[WardrobeItem],
    ) -> list[WardrobeItemRelationship]:
        generated: list[WardrobeItemRelationship] = []
        for left, right in combinations(items, 2):
            relation = _rule_based_relationship(left, right)
            if relation is None:
                continue
            generated.append(
                await self.upsert_relationship(
                    source_item_id=left.id,
                    target_item_id=right.id,
                    relationship_type=relation.relationship_type,
                    score=relation.score,
                    confidence=relation.confidence,
                    source="rule_based",
                    metadata_json=relation.metadata_json,
                )
            )

        # TODO(BE#48): add LLM-generated relationship suggestions here after the
        # prompt contract and review/correction loop exist.
        return generated


class _RuleRelationship:
    def __init__(
        self,
        *,
        relationship_type: str,
        score: float,
        confidence: float,
        metadata_json: dict[str, Any],
    ) -> None:
        self.relationship_type = relationship_type
        self.score = score
        self.confidence = confidence
        self.metadata_json = metadata_json


def _rule_based_relationship(
    left: WardrobeItem,
    right: WardrobeItem,
) -> _RuleRelationship | None:
    left_color = left.color_main.lower() if left.color_main else ""
    right_color = right.color_main.lower() if right.color_main else ""
    if left_color and right_color and frozenset((left_color, right_color)) in COLOR_CONFLICTS:
        return _RuleRelationship(
            relationship_type="style_clash",
            score=-0.55,
            confidence=0.65,
            metadata_json={"rules": ["color_conflict"]},
        )

    score = 0.0
    rules: list[str] = []
    if _has_color_harmony(left_color, right_color):
        score += 0.35
        rules.append("color_harmony")

    formality_delta = _formality_delta(left, right)
    if formality_delta is not None:
        if formality_delta <= 1:
            score += 0.25
            rules.append("formality_match")
        elif formality_delta >= 4:
            score -= 0.35
            rules.append("formality_clash")

    if _tags(left.style_tags) & _tags(right.style_tags):
        score += 0.25
        rules.append("occasion_overlap")

    if not rules:
        return None

    relationship_type = "pairs_well" if score >= 0.35 else "neutral"
    if score <= -0.3:
        relationship_type = "style_clash"
    return _RuleRelationship(
        relationship_type=relationship_type,
        score=_clamp(score, -1.0, 1.0),
        confidence=min(0.9, 0.45 + len(rules) * 0.1),
        metadata_json={"rules": rules},
    )


def _ordered_pair(left_id: uuid.UUID, right_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID]:
    return (left_id, right_id) if str(left_id) <= str(right_id) else (right_id, left_id)


def _has_color_harmony(left_color: str, right_color: str) -> bool:
    if not left_color or not right_color:
        return False
    return left_color == right_color or left_color in NEUTRAL_COLORS or right_color in NEUTRAL_COLORS


def _formality_delta(left: WardrobeItem, right: WardrobeItem) -> int | None:
    if left.formality_score is None or right.formality_score is None:
        return None
    return abs(left.formality_score - right.formality_score)


def _tags(raw: Any) -> set[str]:
    values: object
    if isinstance(raw, dict):
        values = raw.get("tags", [])
    else:
        values = raw
    if not isinstance(values, list):
        return set()
    return {str(value).lower() for value in cast(list[object], values) if isinstance(value, str)}


def _clamp(value: float, low: float, high: float) -> float:
    return min(high, max(low, value))
