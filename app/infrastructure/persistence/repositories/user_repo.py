import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.persistence.models.user import User, UserSettings


class SqlAlchemyUserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get_by_id(self, user_id: uuid.UUID) -> User | None:
        return await self.session.get(User, user_id)

    async def get_or_create(self, user_id: uuid.UUID) -> User:
        existing = await self.session.scalar(select(User).where(User.id == user_id))
        if existing is not None:
            return existing

        user = User(id=user_id)
        self.session.add(user)
        await self.session.flush()
        return user

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
