import uuid

from app.infrastructure.persistence.models.style import UserStyleProfile
from app.infrastructure.persistence.repositories.style_repo import SqlAlchemyStyleRepository


class AddStyleInspiration:
    def __init__(self, repository: SqlAlchemyStyleRepository) -> None:
        self.repository = repository

    async def execute(
        self,
        user_id: uuid.UUID,
        image_url: str | None = None,
        source_url: str | None = None,
    ) -> UserStyleProfile:
        profile = UserStyleProfile(
            user_id=user_id,
            source_type="inspiration_image",
            image_url=image_url,
            source_url=source_url,
            weight=1.0,
            learn_status="queued",
        )
        return await self.repository.add(profile)
