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
    LOCAL_STORAGE_ROOT: str = "/tmp/aidrobe_local_storage"
    LOCAL_STORAGE_BASE_URL: str = "http://localhost:9000/local"
    API_RATE_LIMIT_PER_USER_MINUTE: int = 100
    AUTH_RATE_LIMIT_PER_IP_MINUTE: int = 10
    # Browser clients (web build) need CORS; native apps don't. In dev/test any
    # localhost origin is allowed automatically; in prod, set explicit origins
    # here (comma-separated) to enable CORS for a hosted web client.
    CORS_ALLOW_ORIGINS: str = ""
    VISION_MAX_CALLS_PER_USER_HOUR: int = 60
    VISION_TIMEOUT_SECONDS: float = 10.0
    QWEN_API_KEY: str | None = None
    QWEN_VISION_MODEL: str = "qwen-vl-max"
    QWEN_VISION_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
    # wanx image edit (V4 OOTD poster) — async ImageSynthesis API, same
    # DashScope key as vision. Background-only mask repaint keeps the
    # subject (person + clothes) pixel-locked.
    QWEN_IMAGEEDIT_MODEL: str = "wanx2.1-imageedit"
    QWEN_IMAGEEDIT_BASE_URL: str = (
        "https://dashscope.aliyuncs.com/api/v1/services/aigc/image2image/image-synthesis"
    )
    QWEN_TASK_POLL_BASE_URL: str = "https://dashscope.aliyuncs.com/api/v1/tasks"
    IMAGEEDIT_TIMEOUT_SECONDS: float = 60.0
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

    # Phase B explainability runtime — independent flags so each pipeline
    # (scoring / obstacles / prep) can be rolled out and rolled back alone.
    ENABLE_OUTFIT_SCORES: bool = False
    ENABLE_OBSTACLES: bool = False
    ENABLE_PREP_STEPS: bool = False

    # Phase C async-jobs framework. While dual-write is on, code paths that
    # adopt the framework should ALSO write the legacy artifact (arq job /
    # direct table row) so we can compare outcomes and roll back on divergence.
    ASYNC_JOBS_DUAL_WRITE: bool = True

    # Phase D — gated independently from Phase B explainability so context
    # can be rolled out (e.g. weather provider down → just disable, no need
    # to roll back scoring).
    ENABLE_TODAY_CONTEXT: bool = False
    ENABLE_TODAY_REFRESH: bool = False
    # Total daily_recommendations rows allowed per (user, date), inclusive
    # of the original background-generated rev-1. Enforced when
    # ENABLE_TODAY_REFRESH=True.
    TODAY_REFRESH_DAILY_LIMIT: int = 3

    # Phase E — PIPL-aware account deletion + cost guard.
    # When False, DELETE /v1/me returns 503 (deliberate kill switch so a
    # bad rollout doesn't accidentally let users self-destruct mid-incident).
    ENABLE_USER_PURGE: bool = False
    # Days between deactivation and the hard-purge async-job firing. PIPL
    # doesn't fix a number, but giving users a window to reactivate is
    # standard. 30d aligns with industry norm.
    USER_PURGE_GRACE_DAYS: int = 30
    # HMAC key used to derive the opaque `usr_<22ch>` identifier passed to
    # LLM prompts in place of the raw user_id. MUST be ≥ 32 random bytes
    # in prod. Leaving the default in prod is a safety hazard — the
    # sanitizer module raises on first use if APP_ENV='prod' and this is
    # still the default placeholder.
    LLM_USER_TOKEN_HMAC_KEY: str = "dev-only-change-me-llm-user-token-hmac-key-32b"

    # Phase E5 — Aliyun OSS / STS / CDN. Provider selection is via
    # STORAGE_PROVIDER ("aliyun_oss" wires the OSS adapter); leaving these
    # at their defaults keeps the existing S3 / local paths working.
    ENABLE_OSS_STS: bool = False
    ALIYUN_OSS_REGION: str = "cn-hangzhou"
    ALIYUN_OSS_ENDPOINT: str = ""
    # Default bucket (fallback when a per-purpose bucket env is unset).
    # Keep `ALIYUN_OSS_BUCKET` so the legacy single-bucket dev config still
    # works; production MUST set the per-purpose ones.
    ALIYUN_OSS_BUCKET: str = ""
    ALIYUN_OSS_BUCKET_ITEMS: str = ""
    ALIYUN_OSS_BUCKET_OOTD: str = ""
    ALIYUN_OSS_BUCKET_PRIVATE: str = ""
    ALIYUN_OSS_BUCKET_DERIVED: str = ""
    ALIYUN_OSS_ACCESS_KEY_ID: str = ""
    ALIYUN_OSS_ACCESS_KEY_SECRET: str = ""
    ALIYUN_OSS_PUBLIC_BASE_URL: str = ""
    # STS uses a separate, narrower-scoped credential pair than the OSS
    # service account so a leak here doesn't compromise the buckets too.
    ALIYUN_STS_ACCESS_KEY_ID: str = ""
    ALIYUN_STS_ACCESS_KEY_SECRET: str = ""
    ALIYUN_STS_ROLE_ARN: str = ""
    ALIYUN_STS_TTL_SECONDS: int = 900
    # CDN signing for private-bucket reads. See cdn_url.py for scheme details.
    CDN_BASE_URL: str = ""
    CDN_AUTH_KEY: str = ""
    CDN_SIGN_TTL_SECONDS: int = 1800
    # Whether the `cost_guard` middleware is on. When off, jobs run
    # unconditionally; tokens are still recorded on `async_jobs.cost_*`
    # so the limit can be enabled later without backfill.
    ENABLE_COST_GUARD: bool = False
    # Per-user-per-day caps. Enforced only when ENABLE_COST_GUARD=True.
    LLM_DAILY_TOKEN_LIMIT: int = 50_000
    OCR_DAILY_CALL_LIMIT: int = 100
    TRYON_DAILY_CALL_LIMIT: int = 5
    OOTD_POSTER_DAILY_CALL_LIMIT: int = 5

    # OOTD poster (V4): LLM-generated stylized poster from a user's real
    # wear selfie. Background-only repaint via wanx mask edit; clothes stay
    # pixel-locked. Off by default; each call requires explicit user consent
    # (body image leaves to a third-party model). Data persists regardless of
    # the flag; the flag only gates exposure.
    ENABLE_OOTD_POSTER: bool = False

    # V4 Outfit Calendar — plan/calendar endpoints. Off by default; plans
    # persist regardless of the flag, the flag only gates exposure.
    ENABLE_OUTFIT_CALENDAR: bool = False

    # M16 Travel Packing List — generate/edit endpoints. Off by default;
    # lists persist regardless of the flag, the flag only gates exposure.
    ENABLE_PACKING_LIST: bool = False

    # V4 M21 — half-year / annual wardrobe report (Spotify-Wrapped style).
    # Pure aggregation over existing wear/item data; off by default. The flag
    # only gates exposure of GET /v1/insights/reports/{period} (503 when off).
    ENABLE_WARDROBE_REPORT: bool = False

    # V4 M17 Shopping Sense-Check — purchase evaluation endpoints. Off by
    # default; sense-check history persists regardless of the flag, the flag
    # only gates exposure.
    ENABLE_SENSE_CHECK: bool = False

    # Comma-separated UUIDs allowed to hit `/v1/admin/*`. Empty in dev means
    # every authenticated user is admin (DX); empty in prod means the admin
    # surface is locked off. This is intentional so a misconfigured prod
    # deployment fails closed rather than open.
    ADMIN_USER_IDS: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
