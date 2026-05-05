from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    DATABASE_URL: str = "postgresql+asyncpg://aidrobe:aidrobe@localhost:5432/aidrobe"
    REDIS_URL: str = "redis://localhost:6379/0"
    ARQ_REDIS_URL: str = "redis://localhost:6379/1"
    ARQ_JOB_TIMEOUT_SECONDS: int = 300
    ARQ_MAX_JOBS: int = 50
    APP_ENV: Literal["dev", "test", "prod"] = "dev"
    ENABLE_SCHEDULER: bool = False
    VISION_PROVIDER: str = "fake"
    STORAGE_PROVIDER: str = "local"
    API_RATE_LIMIT_PER_USER_MINUTE: int = 100
    AUTH_RATE_LIMIT_PER_IP_MINUTE: int = 10
    VISION_MAX_CALLS_PER_USER_HOUR: int = 60
    VISION_TIMEOUT_SECONDS: float = 10.0
    QWEN_API_KEY: str | None = None
    QWEN_VISION_MODEL: str = "qwen-vl-max"
    QWEN_VISION_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    S3_BUCKET: str | None = None
    S3_REGION: str = "us-east-1"
    S3_ENDPOINT_URL: str | None = None
    S3_ACCESS_KEY_ID: str | None = None
    S3_SECRET_ACCESS_KEY: str | None = None
    S3_PUBLIC_BASE_URL: str | None = None
    S3_PRESIGN_EXPIRES_SECONDS: int = 900
    LOG_LEVEL: str = "INFO"
    LOG_FORMAT: Literal["json", "console"] = "console"
    SENTRY_DSN: str | None = None
    SENTRY_ENVIRONMENT: str | None = None
    SENTRY_TRACES_SAMPLE_RATE: float = 0.0
    JWT_SECRET: str = "dev-only-change-me-dev-only-change-me"
    JWT_ALGORITHM: str = "HS256"
    JWT_AUDIENCE: str = "aidrobe-api"
    JWT_ISSUER: str = "aidrobe"
    JWT_ACCESS_TOKEN_MINUTES: int = 60
    JWT_REFRESH_TOKEN_DAYS: int = 30
    APPLE_CLIENT_ID: str | None = None
    APPLE_JWKS_TIMEOUT_SECONDS: float = 5.0
    AUTH_APPLE_VERIFY_SIGNATURE: bool = True
    APNS_KEY_ID: str | None = None
    APNS_TEAM_ID: str | None = None
    APNS_BUNDLE_ID: str | None = None
    APNS_AUTH_KEY_PATH: str | None = None
    APNS_USE_SANDBOX: bool = True
    APNS_TIMEOUT_SECONDS: float = 5.0
    LINK_PARSE_TIMEOUT_SECONDS: float = 5.0
    LLM_PROVIDER: str = "none"
    OPENAI_API_KEY: str | None = None
    OPENAI_LLM_MODEL: str = "gpt-5.2"
    QWEN_LLM_MODEL: str = "qwen-turbo"
    QWEN_LLM_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    LLM_TIMEOUT_SECONDS: float = 3.0
    EMBEDDING_PROVIDER: str = "fake"
    CLIP_EMBEDDING_API_KEY: str | None = None
    CLIP_EMBEDDING_BASE_URL: str = "https://api.openai.com/v1/embeddings"
    CLIP_EMBEDDING_TIMEOUT_SECONDS: float = 10.0
    WEATHER_PROVIDER: str = "none"
    QWEATHER_API_KEY: str | None = None
    QWEATHER_BASE_URL: str = "https://devapi.qweather.com"
    OPENWEATHER_API_KEY: str = ""
    WEATHER_CACHE_TTL_SECONDS: int = 3600
    WEATHER_TIMEOUT_SECONDS: float = 5.0


@lru_cache
def get_settings() -> Settings:
    return Settings()
