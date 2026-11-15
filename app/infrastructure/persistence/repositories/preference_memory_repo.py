"""Preference persistence with scoped upserts and evidence revocation fences."""

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import and_, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.infrastructure.persistence.models.ask_conversation import AskConversationState
from app.infrastructure.persistence.models.preference_memory import UserPreferenceMemory
from app.infrastructure.persistence.models.user import User


class PreferenceEvidenceRevoked(ValueError):
    """This evidence predates the user's correction; it must not be relearned."""


class SqlAlchemyPreferenceMemoryRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def lock_user(self, user_id: uuid.UUID) -> User:
        # NO KEY UPDATE serializes memory writes but remains compatible with
        # FK KEY SHARE locks held by concurrent newly-created conversations.
        user = await self.session.scalar(
            select(User).where(User.id == user_id).with_for_update(key_share=True)
            .execution_options(populate_existing=True)
        )
        if user is None:
            raise ValueError("Unknown preference owner")
        return user

    async def _reset_context(self, user: User, at: datetime) -> None:
        user.memory_context_reset_at = at
        # Keep the user's messages, outfit and current factual constraints.
        # Unattributed prose is not safe to reuse after correcting a memory.
        await self.session.execute(
            update(AskConversationState)
            .where(AskConversationState.user_id == user.id)
            .values(summary=None)
        )

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
        scope: dict[str, str] | None = None,
        expires_at: datetime | None = None,
        evidence_at: datetime | None = None,
    ) -> UserPreferenceMemory:
        user = await self.lock_user(user_id)
        now = datetime.now(UTC)
        incoming_at = evidence_at or now
        scope = scope or {}
        revoked_at = await self.session.scalar(
            select(func.max(UserPreferenceMemory.deleted_at)).where(
                UserPreferenceMemory.user_id == user_id,
                UserPreferenceMemory.preference_type == preference_type,
                func.lower(UserPreferenceMemory.preference_value) == preference_value.lower(),
                UserPreferenceMemory.scope == scope,
            )
        )
        barriers = [stamp for stamp in (revoked_at, user.memory_cleared_at) if stamp is not None]
        if barriers and (
            incoming_at <= max(barriers)
            or (source != "explicit_user_statement" and evidence_at is None)
        ):
            raise PreferenceEvidenceRevoked("Preference evidence was revoked")
        if source != "explicit_user_statement" and expires_at is None:
            expires_at = incoming_at + timedelta(days=14)
        insert_stmt = pg_insert(UserPreferenceMemory).values(
            user_id=user_id,
            preference_type=preference_type,
            preference_value=preference_value,
            polarity=polarity,
            confidence=confidence,
            source=source,
            source_message_id=source_message_id,
            scope=scope,
            expires_at=expires_at,
            evidence_at=incoming_at,
        )
        explicit = source == "explicit_user_statement"
        newer_evidence = or_(
            UserPreferenceMemory.evidence_at.is_(None),
            UserPreferenceMemory.evidence_at <= incoming_at,
        )
        stmt = insert_stmt.on_conflict_do_update(
            index_elements=["user_id", "preference_type", "preference_value", "scope"],
            index_where=UserPreferenceMemory.deleted_at.is_(None),
            set_={
                "polarity": polarity,
                "confidence": confidence,
                "source": source,
                "source_message_id": source_message_id,
                "expires_at": expires_at,
                "evidence_at": incoming_at,
                "updated_at": now,
            },
            where=newer_evidence if explicit else and_(
                UserPreferenceMemory.source != "explicit_user_statement", newer_evidence
            ),
        ).returning(UserPreferenceMemory.id)
        row_id = (await self.session.execute(stmt)).scalar_one_or_none()
        changed = row_id is not None
        if row_id is None:
            row_id = await self.session.scalar(
                select(UserPreferenceMemory.id).where(
                    UserPreferenceMemory.user_id == user_id,
                    UserPreferenceMemory.preference_type == preference_type,
                    UserPreferenceMemory.preference_value == preference_value,
                    UserPreferenceMemory.scope == scope,
                    UserPreferenceMemory.deleted_at.is_(None),
                )
            )
        if explicit and changed:
            await self._reset_context(user, now)
        assert row_id is not None
        row = await self.session.get(UserPreferenceMemory, row_id, populate_existing=True)
        assert row is not None
        return row

    async def list_live(self, user_id: uuid.UUID) -> list[UserPreferenceMemory]:
        now = datetime.now(UTC)
        result = await self.session.scalars(
            select(UserPreferenceMemory)
            .where(
                UserPreferenceMemory.user_id == user_id,
                UserPreferenceMemory.deleted_at.is_(None),
                or_(UserPreferenceMemory.expires_at.is_(None), UserPreferenceMemory.expires_at > now),
            )
            .order_by(UserPreferenceMemory.confidence.desc(), UserPreferenceMemory.updated_at.desc())
        )
        return list(result)

    async def learning_cutoffs(self, user_id: uuid.UUID) -> tuple[datetime | None, dict[tuple[str, str], datetime]]:
        """Only post-correction events may support a new global weak signal."""
        reset_at = await self.session.scalar(select(User.memory_cleared_at).where(User.id == user_id))
        revoked = await self.session.execute(
            select(
                UserPreferenceMemory.preference_type,
                func.lower(UserPreferenceMemory.preference_value),
                func.max(UserPreferenceMemory.deleted_at),
            ).where(
                UserPreferenceMemory.user_id == user_id,
                UserPreferenceMemory.deleted_at.is_not(None),
                UserPreferenceMemory.scope == {},
            ).group_by(UserPreferenceMemory.preference_type, func.lower(UserPreferenceMemory.preference_value))
        )
        return reset_at, {(ptype, value): stamp for ptype, value, stamp in revoked if stamp is not None}

    async def soft_delete_all_live(self, user_id: uuid.UUID) -> int:
        user = await self.lock_user(user_id)
        now = datetime.now(UTC)
        user.memory_cleared_at = now
        ids = list(await self.session.scalars(
            update(UserPreferenceMemory)
            .where(UserPreferenceMemory.user_id == user_id, UserPreferenceMemory.deleted_at.is_(None))
            .values(deleted_at=now)
            .returning(UserPreferenceMemory.id)
        ))
        # Advance the watermark even when no rows exist: old events may not
        # have reached the learning threshold yet.
        await self._reset_context(user, now)
        await self.session.flush()
        return len(ids)

    async def soft_delete(self, user_id: uuid.UUID, preference_id: uuid.UUID) -> bool:
        # Foreign/unknown ids remain a no-op even for a nonexistent caller.
        owned = await self.session.scalar(select(UserPreferenceMemory.id).where(
            UserPreferenceMemory.id == preference_id, UserPreferenceMemory.user_id == user_id
        ))
        if owned is None:
            return False
        user = await self.lock_user(user_id)
        row = await self.session.scalar(
            select(UserPreferenceMemory).where(
                UserPreferenceMemory.id == preference_id,
                UserPreferenceMemory.user_id == user_id,
                UserPreferenceMemory.deleted_at.is_(None),
            ).execution_options(populate_existing=True)
        )
        if row is None:
            return False
        now = datetime.now(UTC)
        row.deleted_at = now
        await self._reset_context(user, now)
        await self.session.flush()
        return True

    async def soft_delete_matching(
        self,
        user_id: uuid.UUID,
        *,
        preference_type: str,
        preference_value: str,
        scope: dict[str, str] | None = None,
        evidence_at: datetime | None = None,
    ) -> int:
        """Revoke a named preference even before its first write has finished."""
        user = await self.lock_user(user_id)
        pref_type = preference_type.strip().lower()
        value = preference_value.strip().lower()
        scope = scope or {}
        key: list[ColumnElement[bool]] = [
            UserPreferenceMemory.user_id == user_id,
            UserPreferenceMemory.preference_type == pref_type,
            func.lower(UserPreferenceMemory.preference_value) == value,
            UserPreferenceMemory.scope == scope,
        ]
        now = datetime.now(UTC)
        # The user's turn is the ordering boundary, not when its model response
        # finishes. A slower forget must leave later evidence writable.
        revoked_at = evidence_at or now
        ids = list(await self.session.scalars(
            update(UserPreferenceMemory).where(
                *key,
                UserPreferenceMemory.deleted_at.is_(None),
                or_(UserPreferenceMemory.evidence_at.is_(None), UserPreferenceMemory.evidence_at <= revoked_at),
            ).values(deleted_at=revoked_at)
            .returning(UserPreferenceMemory.id)
        ))
        new_fence = False
        if not ids:
            previous = await self.session.scalar(select(func.max(UserPreferenceMemory.deleted_at)).where(*key))
            if previous is None or previous < revoked_at:
                # A deleted neutral row carries only a revocation fence. It is
                # invisible to memory readers, uses the existing user cascade,
                # and is also consumed by the feedback learner's cutoffs.
                self.session.add(UserPreferenceMemory(
                    user_id=user_id, preference_type=pref_type, preference_value=value,
                    polarity="neutral", confidence=0, source="explicit_user_statement",
                    scope=scope, evidence_at=revoked_at, deleted_at=revoked_at,
                ))
                new_fence = True
        if ids or new_fence:
            await self._reset_context(user, now)
            await self.session.flush()
        return len(ids)
