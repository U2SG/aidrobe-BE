"""BE#46 — repository for `user_preference_memory`.

The write path is an UPSERT on the partial unique index
`uq_user_preference_memory_live (user_id, preference_type, preference_value)
WHERE deleted_at IS NULL`. Per project convention a unique-violation here is a
race, not a bug, so we never select-then-insert — `ON CONFLICT DO UPDATE`
refreshes `polarity`/`confidence`/`source`/`source_message_id` of the live row
in one statement. `deleted_at` is reset to NULL on conflict so re-stating a
previously corrected preference revives it.
"""

import uuid

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.persistence.models.preference_memory import UserPreferenceMemory


class SqlAlchemyPreferenceMemoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def upsert(
        self,
        *,
        user_id: uuid.UUID,
        preference_type: str,
        preference_value: str,
        polarity: str,
        confidence: float,
        source: str,
        source_message_id: uuid.UUID | None = None,
    ) -> UserPreferenceMemory:
        insert_stmt = pg_insert(UserPreferenceMemory).values(
            user_id=user_id,
            preference_type=preference_type,
            preference_value=preference_value,
            polarity=polarity,
            confidence=confidence,
            source=source,
            source_message_id=source_message_id,
        )
        # Prefer explicit user statements: a non-explicit source (feedback /
        # inferred) must NOT clobber a row that an explicit statement set. The
        # conditional `where` skips the UPDATE in exactly that case — the strong
        # explicit row stands. Explicit incoming always wins; non-explicit over
        # non-explicit still merges (last-writer-wins among weak signals).
        existing_is_explicit = UserPreferenceMemory.source == "explicit_user_statement"
        incoming_is_explicit = source == "explicit_user_statement"
        update_predicate = (
            existing_is_explicit.is_(False) if not incoming_is_explicit else None
        )
        stmt = insert_stmt.on_conflict_do_update(
            index_elements=["user_id", "preference_type", "preference_value"],
            index_where=UserPreferenceMemory.deleted_at.is_(None),
            set_={
                "polarity": polarity,
                "confidence": confidence,
                "source": source,
                "source_message_id": source_message_id,
                "deleted_at": None,
                # ON CONFLICT DO UPDATE bypasses the model's onupdate hook, so
                # set updated_at explicitly. Without it, re-stating a preference
                # at the same confidence leaves updated_at stale, and list_live
                # (confidence desc, updated_at desc) won't surface the just-
                # restated row into the capped prompt-visible memory block.
                "updated_at": func.now(),
            },
            where=update_predicate,
        ).returning(UserPreferenceMemory.id)
        result = await self.session.execute(stmt)
        row_id = result.scalar_one_or_none()
        if row_id is None:
            # The conditional update was skipped (a weak source declined to
            # overwrite an explicit row). Return the untouched live row.
            row_id = await self.session.scalar(
                select(UserPreferenceMemory.id).where(
                    UserPreferenceMemory.user_id == user_id,
                    UserPreferenceMemory.preference_type == preference_type,
                    UserPreferenceMemory.preference_value == preference_value,
                    UserPreferenceMemory.deleted_at.is_(None),
                )
            )
            assert row_id is not None  # conflict implies a live row exists
        # ON CONFLICT DO UPDATE writes in the DB but the identity-map copy of an
        # already-loaded row keeps its stale attributes. populate_existing forces
        # a re-read of the freshly-upserted values.
        row = await self.session.get(
            UserPreferenceMemory, row_id, populate_existing=True
        )
        assert row is not None  # just upserted
        return row

    async def list_live(self, user_id: uuid.UUID) -> list[UserPreferenceMemory]:
        """All non-deleted preferences for a user, highest-confidence first so
        the read service can cap to a compact, most-trusted set."""
        result = await self.session.scalars(
            select(UserPreferenceMemory)
            .where(
                UserPreferenceMemory.user_id == user_id,
                UserPreferenceMemory.deleted_at.is_(None),
            )
            .order_by(
                UserPreferenceMemory.confidence.desc(),
                UserPreferenceMemory.updated_at.desc(),
            )
        )
        return list(result)

    async def soft_delete_all_live(self, user_id: uuid.UUID) -> int:
        """BE#83 — user clears ALL learned memory (keeps the account). Soft
        delete so the rows stay auditable; returns how many were cleared."""
        from datetime import UTC, datetime
        from typing import Any, cast

        from sqlalchemy import update
        from sqlalchemy.engine import CursorResult

        result = cast(
            CursorResult[Any],
            await self.session.execute(
                update(UserPreferenceMemory)
                .where(
                    UserPreferenceMemory.user_id == user_id,
                    UserPreferenceMemory.deleted_at.is_(None),
                )
                .values(deleted_at=datetime.now(UTC))
            ),
        )
        await self.session.flush()
        return int(result.rowcount or 0)

    async def soft_delete(
        self, user_id: uuid.UUID, preference_id: uuid.UUID
    ) -> bool:
        """User correction — mark a preference deleted. Scoped to the owner so a
        foreign id is a no-op (returns False). Uses a wall-clock stamp."""
        from datetime import UTC, datetime

        row = await self.session.get(UserPreferenceMemory, preference_id)
        if row is None or row.user_id != user_id or row.deleted_at is not None:
            return False
        row.deleted_at = datetime.now(UTC)
        await self.session.flush()
        return True
