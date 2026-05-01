from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    DATABASE_URL: str = "postgresql+asyncpg://aidrobe:aidrobe@localhost:5432/aidrobe"
    APP_ENV: Literal["dev", "test", "prod"] = "dev"
    ENABLE_SCHEDULER: bool = False
    VISION_PROVIDER: str = "fake"
    STORAGE_PROVIDER: str = "local"
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: Literal["json", "console"] = "console"


@lru_cache
def get_settings() -> Settings:
    return Settings()
