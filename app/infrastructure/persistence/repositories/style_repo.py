import uuid
from typing import cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.persistence.models.style import UserStyleProfile


class SqlAlchemyStyleRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def add(self, profile: UserStyleProfile) -> UserStyleProfile:
        self.session.add(profile)
        await self.session.flush()
        return profile

    async def get_by_id(
        self, profile_id: uuid.UUID, user_id: uuid.UUID
    ) -> UserStyleProfile | None:
        return cast(
            UserStyleProfile | None,
            await self.session.scalar(
                select(UserStyleProfile).where(
                    UserStyleProfile.id == profile_id,
                    UserStyleProfile.user_id == user_id,
                )
            ),
        )

    async def list_inspirations(
        self, user_id: uuid.UUID, limit: int = 20, offset: int = 0
    ) -> list[UserStyleProfile]:
        result = await self.session.scalars(
            select(UserStyleProfile)
            .where(
                UserStyleProfile.user_id == user_id,
                UserStyleProfile.source_type == "inspiration_image",
            )
            .order_by(UserStyleProfile.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list(result)

    async def list_by_user(self, user_id: uuid.UUID) -> list[UserStyleProfile]:
        result = await self.session.scalars(
            select(UserStyleProfile)
            .where(UserStyleProfile.user_id == user_id)
            .order_by(UserStyleProfile.created_at.desc())
        )
        return list(result)

    async def delete(self, profile_id: uuid.UUID, user_id: uuid.UUID) -> bool:
        existing = await self.get_by_id(profile_id, user_id)
        if existing is None:
            return False
        await self.session.delete(existing)
        await self.session.flush()
        return True

    async def upsert_style_tags(
        self, user_id: uuid.UUID, tags: list[str]
    ) -> UserStyleProfile:
        existing = cast(
            UserStyleProfile | None,
            await self.session.scalar(
                select(UserStyleProfile).where(
                    UserStyleProfile.user_id == user_id,
                    UserStyleProfile.source_type == "style_tag",
                )
            ),
        )
        if existing is not None:
            existing.style_tags = {"tags": tags}
            await self.session.flush()
            return existing

        profile = UserStyleProfile(
            user_id=user_id,
            source_type="style_tag",
            style_tags={"tags": tags},
            weight=0.7,
        )
        self.session.add(profile)
        await self.session.flush()
        return profile
