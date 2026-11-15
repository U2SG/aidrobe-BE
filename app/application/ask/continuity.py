"""Carry one accepted design into an existing user-initiated business action."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ask.continuity_types import DesignContextRef, DesignContextSnapshot, LinkedDesignResult
from app.application.ask.design_types import StylingConstraints
from app.application.ask.styling import StylingService, conflict
from app.application.purchase.outfit_composition import CATEGORY_SLOTS
from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.config.settings import get_settings
from app.infrastructure.persistence.models.ask_conversation import AskConversation, AskMessage
from app.infrastructure.persistence.models.packing_list import PackingList
from app.infrastructure.persistence.models.purchase_decision import WardrobeDecision
from app.infrastructure.persistence.models.recommendation import OutfitItem
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.ask_conversation_repo import SqlAlchemyAskConversationRepository


def require_design_context_enabled() -> None:
    if not get_settings().ENABLE_DESIGNER_SESSIONS:
        raise FriendlyException(error_codes.FORBIDDEN, "Designer sessions are not enabled", status_code=503)


def parse_design_context(raw: Any) -> DesignContextSnapshot | None:
    if raw is None:
        return None
    try:
        return DesignContextSnapshot.model_validate(raw)
    except ValidationError:
        return None


def visible_design_context(raw: Any) -> DesignContextSnapshot | None:
    return parse_design_context(raw) if get_settings().ENABLE_DESIGNER_SESSIONS else None


async def load_design_context(
    session: AsyncSession, user_id: uuid.UUID, reference: DesignContextRef,
) -> DesignContextSnapshot:
    require_design_context_enabled()
    service = StylingService(session, user_id)
    conversation = await service.lock_conversation(reference.conversation_id, reference.expected_revision)
    current = await service.current(conversation.id)
    brief = service.brief(conversation, current)
    constraints = brief.constraints if brief is not None else StylingConstraints()
    # Pending conditions remain useful; the earlier outfit is not their result.
    outfit_id = (
        current.outfit_id if current is not None and brief is not None and not brief.needs_outfit_update else None
    )
    return DesignContextSnapshot(
        conversation_id=conversation.id, revision=conversation.styling_revision,
        outfit_id=outfit_id, constraints=constraints,
    )


def require_same_context(saved: DesignContextSnapshot | None, reference: DesignContextRef | None) -> None:
    if reference is None:
        return
    require_design_context_enabled()
    if saved is None or (saved.conversation_id, saved.revision) != (
        reference.conversation_id, reference.expected_revision
    ):
        raise conflict("This result belongs to a different design. Start a new comparison.")


async def preferred_item_ids(session: AsyncSession, context: DesignContextSnapshot) -> set[uuid.UUID]:
    if context.outfit_id is None:
        return set()
    return set(await session.scalars(select(OutfitItem.item_id).where(OutfitItem.outfit_id == context.outfit_id)))


def restrict_design_items(
    items: Sequence[WardrobeItem], context: DesignContextSnapshot, *, preferred: set[uuid.UUID] | None = None,
) -> list[WardrobeItem]:
    """Apply exact exclusions/locks, then use the target style as a tie preference."""
    constraints = context.constraints
    locked = set(constraints.locked_item_ids)
    excluded = set(constraints.excluded_item_ids)
    usable = [item for item in items if item.id not in excluded and item.fit_status != "no_longer_fits"]
    locked_items = [item for item in usable if item.id in locked]
    if {item.id for item in locked_items} != locked:
        raise conflict("A locked piece is no longer available for this design.")
    locked_slots: set[str] = set()
    for item in locked_items:
        slots = set(CATEGORY_SLOTS.get(item.category.lower(), (item.category.lower(),)))
        if locked_slots.intersection(slots):
            raise conflict("The locked pieces occupy the same outfit slot. Adjust the design first.")
        locked_slots.update(slots)
    usable = [item for item in usable if item.id in locked or not locked_slots.intersection(
        CATEGORY_SLOTS.get(item.category.lower(), (item.category.lower(),))
    )]
    target = (constraints.target_style or "").strip().casefold()
    preferred = preferred or set()

    def priority(item: WardrobeItem) -> tuple[int, int, int]:
        tags = [str(tag).casefold() for tag in (item.style_tags or {}).get("tags", [])]
        return -int(item.id in locked), -int(bool(target) and target in tags), -int(item.id in preferred)

    return sorted(usable, key=priority)


def linked_result_from_metadata(metadata: dict[str, Any] | None) -> LinkedDesignResult | None:
    raw = (metadata or {}).get("linked_result")
    if not raw or not get_settings().ENABLE_DESIGNER_SESSIONS:
        return None
    try:
        result = LinkedDesignResult.model_validate(raw)
    except ValidationError:
        return None
    settings = get_settings()
    if result.kind == "purchase_decision" and not settings.ENABLE_V3_PURCHASE_DECISION:
        return None
    if result.kind == "packing_list" and not settings.ENABLE_PACKING_LIST:
        return None
    return result


async def visible_conversation_messages(
    session: AsyncSession, user_id: uuid.UUID, messages: Sequence[AskMessage],
) -> list[AskMessage]:
    """Hide links to disabled or removed resources in both restore and model input."""
    links = {message.id: linked_result_from_metadata(message.message_metadata) for message in messages
             if "linked_result" in (message.message_metadata or {})}
    decision_ids = {link.resource_id for link in links.values() if link and link.kind == "purchase_decision"}
    packing_ids = {link.resource_id for link in links.values() if link and link.kind == "packing_list"}
    existing_decisions = set(await session.scalars(select(WardrobeDecision.id).where(
        WardrobeDecision.user_id == user_id, WardrobeDecision.id.in_(decision_ids),
    ))) if decision_ids else set()
    existing_packing = set(await session.scalars(select(PackingList.id).where(
        PackingList.user_id == user_id, PackingList.id.in_(packing_ids),
    ))) if packing_ids else set()
    visible: list[AskMessage] = []
    for message in messages:
        if message.id not in links:
            visible.append(message)
            continue
        link = links[message.id]
        if link is not None and link.resource_id in (
            existing_decisions if link.kind == "purchase_decision" else existing_packing
        ):
            visible.append(message)
    return visible


async def record_linked_result(
    session: AsyncSession, user_id: uuid.UUID, context: DesignContextSnapshot, result: LinkedDesignResult,
) -> None:
    conversation = await session.scalar(select(AskConversation).where(
        AskConversation.id == context.conversation_id, AskConversation.user_id == user_id,
    ).with_for_update())
    if conversation is None:
        # A saved business result can outlive the conversation it came from.
        return
    existing = await session.scalar(select(AskMessage.id).where(
        AskMessage.user_id == user_id, AskMessage.conversation_id == context.conversation_id,
        AskMessage.message_metadata["linked_result"]["kind"].astext == result.kind,
        AskMessage.message_metadata["linked_result"]["resource_id"].astext == str(result.resource_id),
    ))
    if existing is not None:
        return
    await SqlAlchemyAskConversationRepository(session).append_message(
        conversation=conversation, role="assistant", content=result.summary,
        message_metadata={"linked_result": result.model_dump(mode="json")},
    )
    await session.flush()
