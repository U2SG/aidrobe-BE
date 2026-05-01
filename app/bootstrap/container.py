from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import get_settings
from app.infrastructure.persistence.repositories.user_repo import SqlAlchemyUserRepository
from app.infrastructure.storage.local_storage import LocalObjectStorage
from app.infrastructure.vision.fake_vision import FakeVision


def user_repository(session: AsyncSession) -> SqlAlchemyUserRepository:
    return SqlAlchemyUserRepository(session)


def object_storage() -> LocalObjectStorage:
    settings = get_settings()
    if settings.STORAGE_PROVIDER != "local":
        raise ValueError(f"Unsupported storage provider: {settings.STORAGE_PROVIDER}")
    return LocalObjectStorage()


def vision_service() -> FakeVision:
    settings = get_settings()
    if settings.VISION_PROVIDER != "fake":
        raise ValueError(f"Unsupported vision provider: {settings.VISION_PROVIDER}")
    return FakeVision()
