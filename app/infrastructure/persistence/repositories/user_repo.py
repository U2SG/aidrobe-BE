import uuid
from datetime import UTC, datetime
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.persistence.models.user import User, UserSession, UserSettings


class SqlAlchemyUserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        return await self.session.get(User, user_id)

    async def get_by_apple_subject(self, apple_subject: str) -> User | None:
        return cast(
            User | None,
            await self.session.scalar(select(User).where(User.apple_subject == apple_subject)),
        )

    async def get_or_create(self, user_id: uuid.UUID) -> User:
        existing = await self.session.scalar(select(User).where(User.id == user_id))
        if existing is not None:
            return existing

        user = User(id=user_id)
        self.session.add(user)
        await self.session.flush()
        return user

    async def get_or_create_by_apple_subject(
        self,
        apple_subject: str,
        email: str | None,
    ) -> User:
        existing = await self.get_by_apple_subject(apple_subject)
        if existing is not None:
            if email and existing.email != email:
                existing.email = email
                await self.session.flush()
            return existing

        user = User(apple_subject=apple_subject, email=email)
        self.session.add(user)
        await self.session.flush()
        return user

    async def create_session(
        self,
        user_id: uuid.UUID,
        refresh_token_hash: str,
        expires_at: datetime,
    ) -> UserSession:
        session = UserSession(
            user_id=user_id,
            refresh_token_hash=refresh_token_hash,
            expires_at=expires_at,
        )
        self.session.add(session)
        await self.session.flush()
        return session

    async def get_active_session(self, session_id: uuid.UUID) -> UserSession | None:
        now = datetime.now(UTC)
        return cast(
            UserSession | None,
            await self.session.scalar(
                select(UserSession).where(
                    UserSession.id == session_id,
                    UserSession.revoked_at.is_(None),
                    UserSession.expires_at > now,
                )
            ),
        )

    async def get_active_session_by_refresh_hash(
        self,
        refresh_token_hash: str,
    ) -> UserSession | None:
        now = datetime.now(UTC)
        return cast(
            UserSession | None,
            await self.session.scalar(
                select(UserSession).where(
                    UserSession.refresh_token_hash == refresh_token_hash,
                    UserSession.revoked_at.is_(None),
                    UserSession.expires_at > now,
                )
            ),
        )

    async def get_or_create_settings(self, user_id: uuid.UUID) -> UserSettings:
        await self.get_or_create(user_id)
        existing = await self.session.scalar(
            select(UserSettings).where(UserSettings.user_id == user_id)
        )
        if existing is not None:
            return existing

        settings = UserSettings(user_id=user_id)
        self.session.add(settings)
        await self.session.flush()
        return settings

    async def flush(self) -> None:
        await self.session.flush()
