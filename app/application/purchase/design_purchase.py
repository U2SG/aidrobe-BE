"""Resolve the accepted design and trace the conditions a purchase can actually check."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass, replace
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ask.design_types import StylingConstraints
from app.application.ask.styling import StylingService, conflict
from app.application.purchase.candidate_normalizer import ParsedItem
from app.application.purchase.outfit_composition import CATEGORY_SLOTS, CompositionConstraints, OutfitEvidence
from app.application.purchase.purchase_decision_rules import DecisionReason, PurchaseDecisionOutcome
from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.common.local_time import utc_now
from app.common.style_tags import normalize_style_tags
from app.infrastructure.persistence.models.ask_conversation import AskMessage
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.style_repo import SqlAlchemyStyleRepository


@dataclass(frozen=True)
class PurchaseDesign:
    snapshot: dict[str, Any]
    constraints: StylingConstraints
    composition: CompositionConstraints
    locked_categories: dict[uuid.UUID, str]


async def resolve_purchase_design(
    session: AsyncSession, user_id: uuid.UUID, conversation_id: uuid.UUID, expected_revision: int,
) -> PurchaseDesign:
    service = StylingService(session, user_id)
    conversation = await service.lock_conversation(conversation_id, expected_revision)
    current = await service.current(conversation_id)
    brief = service.brief(conversation, current)
    if brief is None:
        raise conflict("Save design conditions before evaluating a purchase for this conversation.")
    items = await service.validate_constraints(brief.constraints)
    source = None
    if brief.constraints.inspiration_id is not None:
        source = await SqlAlchemyStyleRepository(session).get_by_id(brief.constraints.inspiration_id, user_id)
        if source is None or source.source_type != "inspiration_image":
            raise FriendlyException(error_codes.NOT_FOUND, "Design inspiration is no longer available", 404)
    message = await session.scalar(select(AskMessage).where(
        AskMessage.user_id == user_id, AskMessage.conversation_id == conversation_id, AskMessage.role == "assistant",
    ).order_by(AskMessage.created_at.desc(), AskMessage.id.desc()).limit(1))
    constraints = brief.constraints
    evaluated = ["owned_items", "locked_items", "excluded_items"]
    unverified = ["size_and_fit", "price_and_budget"]
    if constraints.scene is not None:
        evaluated.append("scene_evidence")
    if constraints.target_style:
        evaluated.append("supporting_item_style_tags")
        unverified.append("candidate_style")
    if constraints.target_date is not None:
        unverified.append("target_date_weather")
    if source is not None:
        unverified.append("reference_visual_similarity")
    if constraints.goal or constraints.notes:
        unverified.append("free_text_goals_and_notes")
    if constraints.preference_overrides:
        unverified.append("preference_overrides")
    snapshot = {
        "conversation_id": str(conversation_id), "revision": brief.revision,
        "constraints": constraints.model_dump(mode="json"),
        "styling_revision_id": str(current.id) if current is not None else None,
        "source_message_id": str(message.id) if message is not None else None,
        "source_message_created_at": message.created_at.isoformat() if message is not None else None,
        "inspiration_id": str(source.id) if source is not None else None,
        "inspiration_created_at": source.created_at.isoformat() if source is not None else None,
        "inspiration_learned_at": source.learned_at.isoformat() if source is not None and source.learned_at else None,
        "captured_at": utc_now().isoformat(),
        "evaluated_dimensions": evaluated, "unverified_dimensions": unverified,
    }
    return PurchaseDesign(
        snapshot=snapshot, constraints=constraints,
        composition=CompositionConstraints(
            locked_item_ids=tuple(constraints.locked_item_ids), excluded_item_ids=tuple(constraints.excluded_item_ids),
            scene=constraints.scene,
            style_tags=normalize_style_tags(re.split(r"[,，、;/；]+", constraints.target_style or "")),
        ),
        locked_categories={item.id: item.category for item in items if item.id in constraints.locked_item_ids},
    )


async def contextualize_purchase(
    session: AsyncSession, user_id: uuid.UUID, design: PurchaseDesign, parsed: ParsedItem | None,
    outfit: OutfitEvidence, outcome: PurchaseDecisionOutcome, locale: str,
) -> tuple[PurchaseDecisionOutcome, dict[str, Any]]:
    """Attach only facts that affected composition, and keep unchecked dimensions visible."""
    selected_ids = {item_id for look in (*outfit.outfits, *outfit.alternatives) for item_id in look.item_ids}
    selected = list(await session.scalars(select(WardrobeItem).where(
        WardrobeItem.user_id == user_id, WardrobeItem.id.in_(selected_ids), WardrobeItem.deleted_at.is_(None),
    ))) if selected_ids else []
    candidate_slots = set(CATEGORY_SLOTS.get(parsed.category or "", ())) if parsed is not None else set()
    occupied = set(candidate_slots)
    conflicts: list[str] = []
    for item_id, category in design.locked_categories.items():
        slots = set(CATEGORY_SLOTS.get(category, ()))
        if not slots or occupied.intersection(slots):
            conflicts.append(str(item_id))
        occupied.update(slots)
    scene = design.constraints.scene
    candidate_scenes = parsed.scene_tags or [] if parsed is not None else []
    scene_support = [look.as_payload() for look in outfit.outfits if look.scene == scene] if scene else []
    styles = set(design.composition.style_tags)
    style_matches = {
        str(item.id): sorted(styles.intersection(normalize_style_tags(item.style_tags)))
        for item in selected if styles.intersection(normalize_style_tags(item.style_tags))
    }
    evidence: dict[str, Any] = {
        "candidate_is_owned": False,
        "locked_item_ids": [str(value) for value in design.constraints.locked_item_ids],
        "excluded_item_ids": [str(value) for value in design.constraints.excluded_item_ids],
        "conflicting_locked_item_ids": conflicts,
        "supporting_item_ids": sorted(str(value) for value in selected_ids),
        "requested_scene": scene,
        "candidate_scene_supported": scene in candidate_scenes if scene else None,
        "supporting_scene_evidence": scene_support,
        "requested_style_tags": sorted(styles), "supporting_style_matches": style_matches,
        "unverified_dimensions": design.snapshot["unverified_dimensions"],
    }
    english = locale.startswith("en")
    reasons = list(outcome.reasons)
    if conflicts:
        text = (
            "This candidate conflicts with a piece you asked to keep; the locked piece was not replaced."
            if english else "这件候选与要保留的衣物占用同一位置，无法同时满足当前条件；已保留原衣物。"
        )
        outcome = replace(
            outcome, decision="wait",
            decision_text=("Keep the current pieces and reconsider this purchase." if english
                           else "先保留当前搭配，再考虑这件。"),
            next_actions=("skip", "style_with_existing"),
        )
        reasons = [reason for reason in reasons if reason.kind not in ("compatibility", "extra_purchase_needed")]
        reasons.insert(0, DecisionReason(kind="compatibility", text=text))
    elif scene is not None and scene not in candidate_scenes and not scene_support:
        if outcome.decision == "worth_adding":
            outcome = replace(
                outcome, decision="wait", uncertainty="high",
                decision_text=("Check whether it works for this occasion before buying." if english
                               else "先确认适合这次场景，再决定是否添置。"),
                next_actions=("skip", "style_with_existing"),
            )
        text = (
            "There is no recorded evidence that this candidate and its supporting pieces suit the requested occasion."
            if english else "候选信息和搭配衣物都还没有支持这次场景的记录，不能确认场景适用。"
        )
        reasons = [reason for reason in reasons if reason.kind != "scene_coverage"]
        reasons.insert(0, DecisionReason(kind="scene_coverage", text=text))
    if styles and not style_matches:
        outcome = replace(outcome, uncertainty="high" if outcome.uncertainty == "high" else "medium")
    boundary = outcome.boundary_note
    if design.constraints.target_date is not None:
        boundary += (" Weather for the chosen date has not been checked." if english else "尚未核验目标日期的天气。")
    if styles:
        boundary += (" Style ranking uses existing garment tags; the candidate's style is not verified." if english
                     else "风格排序依据已有衣物的标签，候选本身的风格尚未核验。")
        if not style_matches:
            boundary += (" No supporting garment had a matching style tag." if english
                         else "搭配衣物中暂未找到匹配目标风格的标签。")
    return replace(outcome, reasons=tuple(reasons[:3]), boundary_note=boundary), evidence
