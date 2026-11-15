"""Persisted designer sessions. Every item edit creates a new owned Outfit."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Sequence
from datetime import date
from decimal import Decimal
from typing import Any, cast

from pydantic import ValidationError
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ask.design_types import StylingConstraints
from app.application.recommendations.feasibility import missing_first_outfit_slots
from app.common import error_codes
from app.common.enums import Category, RecommendationGenerationSource
from app.common.exceptions import FriendlyException
from app.common.local_time import utc_now
from app.infrastructure.persistence.models.ask_conversation import AskConversation, AskStylingRevision
from app.infrastructure.persistence.models.ask_session import AskSession
from app.infrastructure.persistence.models.recommendation import DailyRecommendation, Outfit, OutfitItem
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.ask_conversation_repo import SqlAlchemyAskConversationRepository
from app.infrastructure.persistence.repositories.preference_memory_repo import SqlAlchemyPreferenceMemoryRepository
from app.infrastructure.persistence.repositories.recommendation_repo import SqlAlchemyRecommendationRepository


def fingerprint(operation: str, **values: Any) -> str:
    payload = json.dumps({"operation": operation, **values}, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode()).hexdigest()


def conflict(message: str = "This design changed. Reload it before editing.") -> FriendlyException:
    return FriendlyException(error_codes.STATE_CONFLICT, message, status_code=409)


async def lock_request(session: AsyncSession, user_id: uuid.UUID, request_id: uuid.UUID) -> None:
    digest = hashlib.sha256(f"design-request:{user_id}:{request_id}".encode()).digest()[:8]
    await session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": int.from_bytes(digest, "big", signed=True)})


class StylingService:
    def __init__(self, session: AsyncSession, user_id: uuid.UUID) -> None:
        self.session = session
        self.user_id = user_id
        self.conversations = SqlAlchemyAskConversationRepository(session)
        self.recommendations = SqlAlchemyRecommendationRepository(session)

    async def get_revision(self, revision_id: uuid.UUID) -> AskStylingRevision | None:
        return cast(AskStylingRevision | None, await self.session.scalar(
            select(AskStylingRevision).where(
                AskStylingRevision.id == revision_id, AskStylingRevision.user_id == self.user_id
            )
        ))

    async def current(self, conversation_id: uuid.UUID) -> AskStylingRevision | None:
        return cast(AskStylingRevision | None, await self.session.scalar(
            select(AskStylingRevision).where(
                AskStylingRevision.conversation_id == conversation_id,
                AskStylingRevision.user_id == self.user_id,
            ).order_by(AskStylingRevision.revision.desc()).limit(1)
        ))

    async def replay(self, request_id: uuid.UUID, request_fingerprint: str) -> AskStylingRevision | None:
        await lock_request(self.session, self.user_id, request_id)
        row = await self.session.scalar(select(AskStylingRevision).where(
            AskStylingRevision.user_id == self.user_id, AskStylingRevision.request_id == request_id
        ))
        if row is not None:
            if row.request_fingerprint != request_fingerprint:
                raise conflict("request_id was already used for a different action")
            return row
        previous_ask = await self.session.scalar(select(AskSession.id).where(
            AskSession.user_id == self.user_id, AskSession.client_request_id == request_id
        ))
        if previous_ask is not None:
            raise conflict("request_id was already used for a different action")
        return None

    async def lock_conversation(self, conversation_id: uuid.UUID, expected_revision: int) -> AskConversation:
        # Memory deletion locks User before clearing conversation summaries.
        await SqlAlchemyPreferenceMemoryRepository(self.session).lock_user(self.user_id)
        row = await self.session.scalar(select(AskConversation).where(
            AskConversation.id == conversation_id, AskConversation.user_id == self.user_id
        ).with_for_update().execution_options(populate_existing=True))
        if row is None:
            raise FriendlyException(error_codes.NOT_FOUND, "Conversation not found", status_code=404)
        if row.styling_revision != expected_revision:
            raise conflict()
        return row

    async def owned_outfit(self, outfit_id: uuid.UUID) -> Outfit:
        outfit = await self.recommendations.get_outfit_for_user(outfit_id, self.user_id)
        if outfit is None:
            raise FriendlyException(error_codes.NOT_FOUND, "Outfit not found", status_code=404)
        return outfit

    async def validate_items(
        self, item_ids: Sequence[uuid.UUID], constraints: StylingConstraints
    ) -> list[WardrobeItem]:
        if not 2 <= len(item_ids) <= 8 or len(set(item_ids)) != len(item_ids):
            raise FriendlyException(error_codes.VALIDATION_ERROR, "Choose a complete outfit without duplicate items", 422)
        all_ids = set(item_ids) | set(constraints.locked_item_ids) | set(constraints.excluded_item_ids)
        rows = list(await self.session.scalars(select(WardrobeItem).where(
            WardrobeItem.user_id == self.user_id,
            WardrobeItem.id.in_(all_ids),
            WardrobeItem.deleted_at.is_(None),
        ).order_by(WardrobeItem.id).with_for_update(read=True).execution_options(populate_existing=True)))
        by_id = {item.id: item for item in rows}
        if set(by_id) != all_ids:
            raise FriendlyException(error_codes.NOT_FOUND, "One or more wardrobe items are unavailable", 404)
        items = [by_id[item_id] for item_id in item_ids]
        if any(item.status != "wearable" or item.fit_status == "no_longer_fits" for item in items):
            raise conflict("A selected piece is no longer wearable. Choose a replacement.")
        if set(constraints.excluded_item_ids).intersection(item_ids):
            raise conflict("The outfit includes an excluded piece")
        if not set(constraints.locked_item_ids).issubset(item_ids):
            raise conflict("The outfit must retain every locked piece")
        categories = [item.category for item in items]
        core = [category for category in categories if category in {entry.value for entry in Category}]
        if (missing_first_outfit_slots(categories) or len(set(core)) != len(core)
                or ("dress" in core and {"top", "bottom"}.intersection(core))):
            raise FriendlyException(error_codes.VALIDATION_ERROR, "The outfit has missing or conflicting slots", 422)
        return items

    async def _outfit(self, items: list[WardrobeItem], *, title: str, reason: str) -> Outfit:
        now = utc_now()
        outfit = Outfit(
            user_id=self.user_id, daily_recommendation_id=None, outfit_type="stylish",
            title=title[:120], reason_text=reason[:512], score_total=Decimal("0"),
            rank_order=0, time_sensitivity="standard", created_at=now,
        )
        self.session.add(outfit)
        await self.session.flush()
        self.session.add_all([
            OutfitItem(outfit_id=outfit.id, item_id=item.id, role=item.category, display_order=index, created_at=now)
            for index, item in enumerate(items)
        ])
        await self.session.flush()
        return outfit

    async def _save(
        self, conversation: AskConversation, outfit: Outfit, constraints: StylingConstraints,
        request_id: uuid.UUID, request_fingerprint: str, daily: DailyRecommendation | None = None,
    ) -> AskStylingRevision:
        conversation.styling_revision += 1
        row = AskStylingRevision(
            conversation_id=conversation.id, user_id=self.user_id, revision=conversation.styling_revision,
            outfit_id=outfit.id, constraints=constraints.model_dump(mode="json"),
            request_id=request_id, request_fingerprint=request_fingerprint,
            status="adopted" if daily is not None else "draft",
            recommendation_id=daily.id if daily is not None else None,
            recommendation_revision=daily.revision if daily is not None else None,
            recommendation_date=daily.recommendation_date if daily is not None else None,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def save_draft(
        self, *, conversation_id: uuid.UUID, expected_revision: int, item_ids: Sequence[uuid.UUID],
        constraints: StylingConstraints, request_id: uuid.UUID, request_fingerprint: str,
        title: str, reason: str,
    ) -> AskStylingRevision:
        conversation = await self.lock_conversation(conversation_id, expected_revision)
        items = await self.validate_items(item_ids, constraints)
        outfit = await self._outfit(items, title=title, reason=reason)
        return await self._save(conversation, outfit, constraints, request_id, request_fingerprint)

    async def _replacement(
        self, base: Outfit, constraints: StylingConstraints, replace: tuple[uuid.UUID, uuid.UUID] | None,
    ) -> list[WardrobeItem]:
        ids = [item.item_id for item in sorted(base.items, key=lambda entry: entry.display_order)]
        if replace is not None:
            item_id, replacement_id = replace
            if item_id not in ids:
                raise conflict("The piece to replace is not in this outfit")
            if item_id in constraints.locked_item_ids:
                raise conflict("Unlock this piece before replacing it")
            old = next(item for item in base.items if item.item_id == item_id)
            replacement = await self.session.scalar(select(WardrobeItem).where(
                WardrobeItem.id == replacement_id, WardrobeItem.user_id == self.user_id,
                WardrobeItem.deleted_at.is_(None),
            ))
            if replacement is None:
                raise FriendlyException(error_codes.NOT_FOUND, "Replacement item not found", 404)
            if replacement.category != old.role:
                raise FriendlyException(error_codes.VALIDATION_ERROR, "Replace a piece with one from the same slot", 422)
            ids[ids.index(item_id)] = replacement_id
        return await self.validate_items(ids, constraints)

    async def revise(
        self, *, conversation_id: uuid.UUID, expected_revision: int, request_id: uuid.UUID,
        replace: tuple[uuid.UUID, uuid.UUID] | None, locked_item_ids: list[uuid.UUID] | None,
        excluded_item_ids: list[uuid.UUID] | None,
    ) -> AskStylingRevision:
        request_hash = fingerprint("revise", conversation_id=conversation_id, expected_revision=expected_revision,
                                   replace=replace, locked_item_ids=locked_item_ids, excluded_item_ids=excluded_item_ids)
        if replay := await self.replay(request_id, request_hash):
            return replay
        conversation = await self.lock_conversation(conversation_id, expected_revision)
        current = await self.current(conversation_id)
        if current is None:
            raise conflict("Create a design before editing it")
        raw = dict(current.constraints)
        if locked_item_ids is not None:
            raw["locked_item_ids"] = locked_item_ids
        if excluded_item_ids is not None:
            raw["excluded_item_ids"] = excluded_item_ids
        try:
            constraints = StylingConstraints.model_validate(raw)
        except ValidationError as exc:
            raise FriendlyException(error_codes.VALIDATION_ERROR, "Conflicting design constraints", 422) from exc
        base = await self.owned_outfit(current.outfit_id)
        items = await self._replacement(base, constraints, replace)
        outfit = await self._outfit(items, title=base.title, reason="已按你的选择更新搭配，其余单品保持不变。")
        return await self._save(conversation, outfit, constraints, request_id, request_hash)

    async def _adopt_outfit(self, outfit: Outfit, constraints: StylingConstraints, day: date) -> DailyRecommendation:
        await self.recommendations.lock_daily_write(self.user_id, day)
        if outfit.daily_recommendation_id is not None:
            daily = await self.recommendations.get_daily(self.user_id, day)
            if daily is None or daily.id != outfit.daily_recommendation_id:
                raise conflict("This outfit was already adopted elsewhere. Start a new version to adopt it again.")
            return daily
        daily = await self.recommendations.archive_current_and_revise(DailyRecommendation(
            user_id=self.user_id, recommendation_date=day, status="ready",
            generation_source=RecommendationGenerationSource.DESIGNER.value,
            context_json={"constraints": constraints.notes, "goal": constraints.goal,
                          "target_style": constraints.target_style},
        ))
        outfit.daily_recommendation_id = daily.id
        await self.session.flush()
        return daily

    async def adopt(
        self, *, conversation_id: uuid.UUID, expected_revision: int, request_id: uuid.UUID,
        day: date | None = None, default_day: date | None = None,
    ) -> AskStylingRevision:
        request_hash = fingerprint("adopt", conversation_id=conversation_id, expected_revision=expected_revision, date=day)
        if replay := await self.replay(request_id, request_hash):
            return replay
        conversation = await self.lock_conversation(conversation_id, expected_revision)
        current = await self.current(conversation_id)
        if current is None:
            raise conflict("Create a design before adopting it")
        constraints = StylingConstraints.model_validate(current.constraints)
        outfit = await self.owned_outfit(current.outfit_id)
        await self.validate_items([item.item_id for item in outfit.items], constraints)
        target = day or constraints.target_date or default_day or utc_now().date()
        daily = await self._adopt_outfit(outfit, constraints, target)
        return await self._save(conversation, outfit, constraints, request_id, request_hash, daily)

    async def revise_outfit(
        self, *, outfit_id: uuid.UUID, request_id: uuid.UUID,
        item_id: uuid.UUID, replacement_item_id: uuid.UUID,
    ) -> AskStylingRevision:
        request_hash = fingerprint("revise_outfit", outfit_id=outfit_id, item_id=item_id,
                                   replacement_item_id=replacement_item_id)
        if replay := await self.replay(request_id, request_hash):
            return replay
        await SqlAlchemyPreferenceMemoryRepository(self.session).lock_user(self.user_id)
        base = await self.owned_outfit(outfit_id)
        source = await self.session.scalar(select(AskStylingRevision).where(
            AskStylingRevision.user_id == self.user_id, AskStylingRevision.outfit_id == outfit_id,
        ).order_by(AskStylingRevision.created_at.desc(), AskStylingRevision.revision.desc()).limit(1))
        current = await self.current(source.conversation_id) if source is not None else None
        if base.daily_recommendation_id is None:
            if current is None or current.outfit_id != outfit_id:
                raise conflict("This draft is no longer current. Reload the conversation.")
            conversation = await self.lock_conversation(current.conversation_id, current.revision)
        elif current is not None and current.outfit_id == outfit_id:
            conversation = await self.lock_conversation(current.conversation_id, current.revision)
        else:
            conversation = await self.conversations.create_conversation(user_id=self.user_id, title=base.title)
        constraints = StylingConstraints.model_validate(current.constraints) if (
            current is not None and current.outfit_id == outfit_id
        ) else StylingConstraints()
        source_daily: DailyRecommendation | None = None
        if base.daily_recommendation_id is not None:
            source_daily = await self.session.get(DailyRecommendation, base.daily_recommendation_id)
            if source_daily is None or source_daily.user_id != self.user_id:
                raise conflict("This outfit is no longer available")
            await self.recommendations.lock_daily_write(self.user_id, source_daily.recommendation_date)
            daily = await self.recommendations.get_daily(self.user_id, source_daily.recommendation_date)
            if daily is None or daily.id != source_daily.id:
                raise conflict("Today's outfit changed. Reload it before replacing a piece.")
        items = await self._replacement(base, constraints, (item_id, replacement_item_id))
        outfit = await self._outfit(items, title=base.title, reason="已按你的选择更换单品，其余搭配保持不变。")
        adopted = await self._adopt_outfit(outfit, constraints, source_daily.recommendation_date) if source_daily else None
        return await self._save(conversation, outfit, constraints, request_id, request_hash, adopted)
