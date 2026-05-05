import uuid

from app.infrastructure.persistence.models.style import UserStyleProfile
from app.infrastructure.persistence.repositories.style_repo import SqlAlchemyStyleRepository


class AddStyleInspirationBatch:
    def __init__(self, repository: SqlAlchemyStyleRepository) -> None:
        self.repository = repository

    async def execute(self, user_id: uuid.UUID, urls: list[str]) -> list[UserStyleProfile]:
        profiles = []
        for url in urls:
            profile = UserStyleProfile(
                user_id=user_id,
                source_type="inspiration_image",
                image_url=url,
                weight=1.0,
                learn_status="queued",
            )
            await self.repository.add(profile)
            profiles.append(profile)
        return profiles
