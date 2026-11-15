import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.persistence.models.ask_session import AskSession
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.wardrobe_repo import load_wardrobe_image_sources


class SqlAlchemyAskRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def create(
        self,
        *,
        user_id: uuid.UUID,
        question: str,
        answer: str | None,
        item_refs: list[str],
        context: dict[str, Any] | None,
        tokens_used: int | None,
    ) -> AskSession:
        row = AskSession(
            user_id=user_id,
            question=question,
            answer=answer,
            item_refs=item_refs,
            context=context,
            tokens_used=tokens_used,
        )
        self.session.add(row)
        await self.session.flush()
        return row

    async def list_for_user(
        self,
        user_id: uuid.UUID,
        *,
        limit: int,
        offset: int,
        focused_item_id: uuid.UUID | None = None,
    ) -> list[AskSession]:
        stmt = (
            select(AskSession)
            .where(AskSession.user_id == user_id)
            .order_by(AskSession.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        if focused_item_id is not None:
            # BE#29 — focused history = Asks the user made WHILE focused on this
            # item, i.e. the persisted context snapshot's `focused_item_id`.
            # We deliberately do NOT also match `item_refs.contains(id)`:
            # post-#28 that signal means "the model cited this item", and pre-#28
            # rows still carry the full candidate set (no backfill migration), so
            # matching it would leak unrelated old Asks into the scoped view.
            # Sessions without a snapshot focus stay in global history only.
            target = str(focused_item_id)
            stmt = stmt.where(AskSession.context["snapshot"]["focused_item_id"].astext == target)
        result = await self.session.scalars(stmt)
        return list(result)

    async def fetch_item_ref_snapshots(
        self, user_id: uuid.UUID, item_ids: list[uuid.UUID]
    ) -> list[tuple[WardrobeItem, str | None, str | None]]:
        """BE#29 — batch-load the wardrobe items behind a set of item_refs.

        Batch both owned rows and their selected cover sources so the read
        path can sign thumbnails at request time."""
        if not item_ids:
            return []
        items = list(
            await self.session.scalars(
                select(WardrobeItem).where(
                    WardrobeItem.id.in_(item_ids),
                    WardrobeItem.user_id == user_id,
                    WardrobeItem.deleted_at.is_(None),
                )
            )
        )
        images = await load_wardrobe_image_sources(self.session, items)
        return [(item, *images[item.id][0]) for item in items]
