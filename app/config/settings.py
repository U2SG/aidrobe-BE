from functools import lru_cache
from typing import Literal, Self

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.common.local_time import validate_timezone_name


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    DATABASE_URL: str = "postgresql+asyncpg://aidrobe:aidrobe@localhost:5432/aidrobe"
    REDIS_URL: str = "redis://localhost:6379/0"
    ARQ_REDIS_URL: str = "redis://localhost:6379/1"
    ARQ_JOB_TIMEOUT_SECONDS: int = 300
    ARQ_MAX_JOBS: int = 50
    APP_ENV: Literal["dev", "test", "prod"] = "dev"
    DEFAULT_TIMEZONE: str = "Asia/Shanghai"
    ENABLE_SCHEDULER: bool = False
    VISION_PROVIDER: str = "fake"
    STORAGE_PROVIDER: str = "local"
    LOCAL_STORAGE_ROOT: str = "/tmp/aidrobe_local_storage"
    LOCAL_STORAGE_BASE_URL: str = "http://localhost:9000/local"
    API_RATE_LIMIT_PER_USER_MINUTE: int = 100
    API_RATE_LIMIT_PER_IP_MINUTE: int = 30
    AUTH_RATE_LIMIT_PER_IP_MINUTE: int = 10
    INVITE_LOGIN_CODES: str = ""
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
    # M18 — OCR pipeline for receipt / order-screenshot / shopping-email imports.
    # Mirrors VISION_PROVIDER: "fake" (default, no key) or "qwen" (Qwen-VL OCR).
    OCR_PROVIDER: str = "fake"
    OCR_TIMEOUT_SECONDS: float = 15.0
    QWEN_OCR_MODEL: str = "qwen-vl-ocr"
    QWEN_OCR_BASE_URL: str = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
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
    # Ask answers a free-form question over up to _MAX_ITEMS wardrobe items
    # (plus a JSON-output instruction), so it needs a far more generous budget
    # than the short, template-shaped `polish_reason` call that uses
    # LLM_TIMEOUT_SECONDS. Used by the providers' `ask` path only.
    LLM_ASK_TIMEOUT_SECONDS: float = 12.0
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
    # BE#26 — route import-task processing through job_runtime/arq (lease /
    # retry / dead-letter) instead of FastAPI BackgroundTasks. Off = legacy
    # in-request BackgroundTasks path (unchanged). The import_tasks row is
    # written regardless of the flag, so flipping back is a clean rollback.
    ENABLE_IMPORT_ASYNC_JOBS: bool = False

    # Phase D — gated independently from Phase B explainability so context
    # can be rolled out (e.g. weather provider down → just disable, no need
    # to roll back scoring).
    ENABLE_TODAY_CONTEXT: bool = False
    ENABLE_TODAY_REFRESH: bool = False
    # BE#37/#38 — surface grounded {type, text} reasoning on Today outfits.
    # Reasoning is persisted at generation time regardless; this flag only
    # gates its exposure in the response, so flipping back is a clean rollback.
    ENABLE_TODAY_REASONING: bool = False
    # M15 Ask Aidrobe (LLM Q&A). Off by default; data persists regardless of
    # the flag, the flag only gates endpoint exposure (503 when off). Pair
    # with LLM_PROVIDER (qwen/openai) and ENABLE_COST_GUARD for quotas.
    ENABLE_ASK_AIDROBE: bool = False
    # When on (default), Ask requests structured JSON ({answer,
    # referenced_items}) under provider JSON mode and `item_refs` becomes the
    # server-validated subset the model actually cited (positions intersected
    # with the candidate set) — trustworthy by default. Requires a configured
    # LLM provider (else Ask 503s regardless). Off = legacy behavior:
    # `item_refs` lists every item fed to the prompt; set False to roll back.
    ENABLE_ASK_STRUCTURED_OUTPUT: bool = True
    # BE#47 - multi-turn conversational Ask refinement. Conversation rows and
    # state are persisted regardless; this flag only exposes conversation_id and
    # structured deltas in the API response.
    ENABLE_ASK_CONVERSATIONS: bool = False
    # Total daily_recommendations rows allowed per (user, date), inclusive
    # of the original background-generated rev-1. Enforced when
    # ENABLE_TODAY_REFRESH=True. Set <= 0 to disable the cap entirely.
    TODAY_REFRESH_DAILY_LIMIT: int = 3
    # BE#39 — when on, recommendation generation also avoids repeating recently
    # RECOMMENDED outfits/hero items (not just recently worn): exact-outfit
    # repeats within the window are hard-excluded (relaxed for small wardrobes)
    # and recently-recommended hero items are de-prioritized. Off = legacy
    # worn-only diversity.
    ENABLE_RECOMMENDATION_DIVERSITY: bool = False
    # BE#82 — infer an indoor-main climate tendency from repeated temperature
    # mismatches + commute exposure, and let it nudge outfit ranking (prefer a
    # removable outer layer) + surface an indoor_climate_inference reason. Off →
    # generation is byte-identical to legacy (no inference, no score, no reason).
    ENABLE_INDOOR_CLIMATE_INFERENCE: bool = False
    # BE#78 — accept a Today refresh context override (chip / free text), map it
    # to session-scoped generation constraints (exclude dress, formal, comfort,
    # removable layer, low-key, reduce-weather), apply them to filtering/ranking,
    # and echo applied_context. Off → override is ignored (legacy refresh).
    ENABLE_CONTEXT_OVERRIDES: bool = False
    # BE#80 — surface at most one "you haven't worn this in a while, and it fits
    # today" resurface insight on the Today response, from forgotten + season-
    # suitable wardrobe items. Off → resurface_insight is always absent.
    ENABLE_RESURFACE_INSIGHT: bool = False
    # BE#77 — date-aware Today: GET /today may request tomorrow (evening mode),
    # generating against tomorrow's context (forecast weather when available;
    # honest no-weather degrade otherwise) instead of reusing today's stale
    # recommendation. Off → target_date is ignored (legacy today behavior).
    ENABLE_DATE_AWARE_TODAY: bool = False
    # BE#83 — profile data-trust surface: review / correct / clear learned
    # preference memory, and clear all app data while keeping the account.
    # Destructive endpoints fail closed (403) when off.
    ENABLE_DATA_TRUST: bool = False
    # Days an item must go unworn before it's a resurface candidate.
    RESURFACE_MIN_UNWORN_DAYS: int = 30
    # Look-back window (days) for the diversity checks above. Applied ONLY when
    # ENABLE_RECOMMENDATION_DIVERSITY is on; with the flag off the worn-similarity
    # penalty keeps its legacy fixed 7-day window regardless of this value.
    RECOMMENDATION_DIVERSITY_WINDOW_DAYS: int = 7

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

    # V4 item cutout — gpt-image-2 product-shot extraction on import. Async via
    # job_runtime (job_type "cutout"); off by default. Data (the item + its
    # original image) persists regardless; the flag gates the cutout enqueue +
    # spend, so flipping back to False is a clean rollback (items just keep the
    # original photo as cover). Provider mirrors the vision switch: "fake" (no
    # key, returns the source unchanged) or "openai" (gpt-image-2 images/edits).
    ENABLE_ITEM_CUTOUT: bool = False
    IMAGE_CUTOUT_PROVIDER: str = "fake"
    OPENAI_IMAGE_MODEL: str = "gpt-image-2"
    # low is the cost/quality sweet spot (~$0.024/img, ~35s) — medium/high add
    # marginal polish at 4x/14x the cost. See docs + the cutout bake-off.
    IMAGE_CUTOUT_QUALITY: str = "low"
    IMAGE_CUTOUT_TIMEOUT_SECONDS: float = 180.0
    # Per-user-per-day call cap (call-count quota, like tryon/ootd). Enforced
    # only when ENABLE_COST_GUARD=True.
    ITEM_CUTOUT_DAILY_LIMIT: int = 50

    # V4 Outfit Calendar — plan/calendar endpoints. Off by default; plans
    # persist regardless of the flag, the flag only gates exposure.
    ENABLE_OUTFIT_CALENDAR: bool = False

    # M16 Travel Packing List — generate/edit endpoints. Off by default;
    # lists persist regardless of the flag, the flag only gates exposure.
    ENABLE_PACKING_LIST: bool = False
    # BE#68 — travel packing PLANNER enrichment: per-day outfit plan (via the
    # recommendation engine) + missing-item warnings + weather/occasion reasoning,
    # persisted in packing_lists.plan_json. Off by default; gates both the
    # enrichment and its persistence so legacy packing behavior is the clean
    # rollback. Requires ENABLE_PACKING_LIST for the endpoint to be reachable.
    ENABLE_PACKING_PLANNER: bool = False

    # V4 M21 — half-year / annual wardrobe report (Spotify-Wrapped style).
    # Pure aggregation over existing wear/item data; off by default. The flag
    # only gates exposure of GET /v1/insights/reports/{period} (503 when off).
    ENABLE_WARDROBE_REPORT: bool = False
    # BE#49 wardrobe health score. Pure aggregation over existing item and
    # wear-history data; endpoint returns 503 while disabled.
    ENABLE_WARDROBE_HEALTH: bool = False

    # V4 M17 Shopping Sense-Check — purchase evaluation endpoints. Off by
    # default; sense-check history persists regardless of the flag, the flag
    # only gates exposure.
    ENABLE_SENSE_CHECK: bool = False
    # BE#69 — wardrobe-aware purchase evaluator: enrich the sense-check verdict
    # with redundancy analysis, grounded outfit samples, structured wardrobe-gap
    # evidence, decision factors, confidence, and a cost-per-wear hint. Additive
    # (persisted in the existing payload JSONB); off → legacy verdict, clean
    # rollback. Requires ENABLE_SENSE_CHECK for the endpoint to be reachable.
    ENABLE_PURCHASE_EVALUATOR: bool = False

    # V3 P0 purchase decision (docs/v3-p0-contract.md §2). Gates the
    # /v1/purchase-candidates* and /v1/wardrobe-decisions/{id} surfaces — none
    # of which exist yet (V3-003 is persistence only; V3-004/V3-007/V3-008 add
    # them). Candidates and decision snapshots persist regardless of the flag,
    # so flipping it back is a clean rollback.
    ENABLE_V3_PURCHASE_DECISION: bool = False

    # V3 P0 avatar (docs/v3-p0-contract.md §2, §4). Gates the
    # /v1/avatar-profile* and /v1/avatar-candidates/* surfaces — none of which
    # exist yet (V3-011 is persistence only; V3-012 detects candidates and
    # V3-013 adds the endpoints). Profiles, source assets and versions persist
    # regardless of the flag, so flipping it back is a clean rollback.
    # NOTE: contract §2 rule 5 also makes ENABLE_V3_STATIC_TRYON=true with this
    # flag false a startup error. That clause is deliberately absent from
    # `validate_feature_dependencies` below because ENABLE_V3_STATIC_TRYON does
    # not exist yet — the combination is unreachable. It lands with that flag.
    ENABLE_V3_AVATAR: bool = False

    # --- Avatar retention (V3-011 human gate) ---------------------------
    # HUMAN GATE: the final durations require privacy approval before
    # production enablement; these defaults are placeholders, and the rule the
    # gate constrains is that the code must read *configurable* values rather
    # than hard-code a number (see app/application/avatar/retention.py, which
    # is the only place these are interpreted).
    #
    # Source photos are the sensitive input, kept minimum-necessary: 30d
    # matches the existing `aidrobe-private` bucket policy for try-on body
    # images in app/config/retention.yaml, so the two do not contradict each
    # other. Retired avatar versions are derived, cheap to regenerate while a
    # consented source still exists, and are kept only long enough for a
    # `replace` to be reversible.
    AVATAR_SOURCE_RETENTION_DAYS: int = 30
    AVATAR_RETIRED_VERSION_RETENTION_DAYS: int = 30

    # V4 M19 — PDF / magazine multi-page style ingest. When False, POST
    # /v1/style/sources returns 503 (kill switch). The parent row is still
    # persisted regardless; only the async rasterize/ingest is gated so a bad
    # rollout can't burn poppler/storage cost. Requires the poppler system
    # binary (pdf2image dep) on the worker host to actually run.
    ENABLE_PDF_STYLE_INGEST: bool = False

    # Real visual style learning. When on (and VISION_PROVIDER != fake),
    # the style-learn worker runs Qwen-VL over an inspiration image to extract
    # style_tags / color_palette, persisted into style_tags so they drive the
    # recommendation template matcher (style_context). Off → the legacy URL-domain
    # tag heuristic. Requires QWEN_API_KEY; falls back to the heuristic on any
    # vision error so user inspirations never fail to "learn".
    ENABLE_VISION_STYLE_LEARNING: bool = False

    # M18 — receipt / order-screenshot / shopping-email import pipeline.
    # When False, POST /v1/import-tasks with receipt input_types returns 400.
    ENABLE_RECEIPT_IMPORT: bool = False

    # BE#46 — persistent user preference memory. When on, Ask reads the
    # `{positive, negative}` memory into its prompt context and Today
    # recommendations let explicit NEGATIVE preferences (e.g. a disliked color)
    # down-rank matching outfits. Preference rows are written regardless of the
    # flag (controlled write path); the flag only gates the read/injection, so
    # flipping back to False is a clean rollback.
    ENABLE_PREFERENCE_MEMORY: bool = False
    # BE#48 wardrobe relationship graph. Rule-based edges are 100% derived from
    # current item metadata, so — unlike user data — they are NOT persisted when
    # this flag is off (no passive backfill); they are re-derived cheaply when it
    # flips on. The flag therefore gates both rule-based generation/persistence
    # and the ranking boost/penalty in Today recommendations. User/feedback edges
    # (when added) are user data and follow the persist-regardless convention.
    ENABLE_WARDROBE_GRAPH: bool = False
    # BE#67 feedback learning. Converts recent feedback events into (a) a
    # ranking suppression signal for dismissed/disliked outfit item-sets and
    # (b) controlled WEAK preference-memory updates from repeated feedback. The
    # weak WRITE is new behavior, so — unlike the read-gated preference convention
    # — this flag gates the write too, keeping flag-off a clean rollback. The
    # suppression signal is recomputed at generation time (no persistence).
    ENABLE_FEEDBACK_LEARNING: bool = False

    # Comma-separated UUIDs allowed to hit `/v1/admin/*`. Empty in dev means
    # every authenticated user is admin (DX); empty in prod means the admin
    # surface is locked off. This is intentional so a misconfigured prod
    # deployment fails closed rather than open.
    ADMIN_USER_IDS: str = ""

    @field_validator("DEFAULT_TIMEZONE")
    @classmethod
    def validate_default_timezone(cls, value: str) -> str:
        return validate_timezone_name(value)

    @model_validator(mode="after")
    def validate_feature_dependencies(self) -> Self:
        if self.ENABLE_CONTEXT_OVERRIDES and not self.ENABLE_TODAY_REFRESH:
            raise ValueError(
                "ENABLE_CONTEXT_OVERRIDES requires ENABLE_TODAY_REFRESH"
            )
        if self.ENABLE_PACKING_PLANNER and not self.ENABLE_PACKING_LIST:
            raise ValueError(
                "ENABLE_PACKING_PLANNER requires ENABLE_PACKING_LIST"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
