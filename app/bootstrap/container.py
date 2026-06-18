from urllib.parse import urlparse

from sqlalchemy.ext.asyncio import AsyncSession

try:
    from arq.connections import RedisSettings, create_pool
except ImportError:  # arq is optional at runtime
    RedisSettings = None  # type: ignore[assignment, misc]
    create_pool = None  # type: ignore[assignment]

from app.application.ports.embedding import EmbeddingPort
from app.application.ports.image_cutout import ImageCutoutPort
from app.application.ports.llm import LlmPort
from app.application.ports.ocr import OcrPort
from app.application.ports.sts_token import StsTokenPort
from app.application.ports.weather import WeatherPort
from app.config.settings import get_settings
from app.infrastructure.cache.client import get_redis_client
from app.infrastructure.importers.router import RoutedLinkParser
from app.infrastructure.persistence.repositories.user_repo import SqlAlchemyUserRepository
from app.infrastructure.push.apns_provider import ApnsProvider
from app.infrastructure.storage.local_storage import LocalObjectStorage
from app.infrastructure.storage.s3_storage import S3ObjectStorage
from app.infrastructure.vision.fake_image_gen import FakeImageGeneration
from app.infrastructure.vision.fake_vision import FakeVision
from app.infrastructure.vision.qwen_vision import QwenVision
from app.infrastructure.vision.rate_limit import InMemoryVisionRateLimiter
from app.infrastructure.vision.wanx_image_gen import WanxImageGeneration
from app.infrastructure.weather.openweather import OpenWeatherProvider
from app.infrastructure.weather.qweather import QWeatherProvider

_vision_rate_limiter: InMemoryVisionRateLimiter | None = None


def user_repository(session: AsyncSession) -> SqlAlchemyUserRepository:
    return SqlAlchemyUserRepository(session)


def object_storage() -> LocalObjectStorage | S3ObjectStorage:
    settings = get_settings()
    if settings.STORAGE_PROVIDER == "local":
        return LocalObjectStorage(
            base_url=settings.LOCAL_STORAGE_BASE_URL,
            storage_root=settings.LOCAL_STORAGE_ROOT,
        )
    if settings.STORAGE_PROVIDER == "s3":
        return S3ObjectStorage(settings)
    if settings.STORAGE_PROVIDER == "aliyun_oss":
        # Lazy import — `oss2` is an optional runtime dep loaded only when
        # the operator actually selects this provider, so dev without the
        # SDK installed still works.
        from app.infrastructure.storage.aliyun_oss import AliyunOssStorage

        return AliyunOssStorage(settings)  # type: ignore[return-value]
    raise ValueError(f"Unsupported storage provider: {settings.STORAGE_PROVIDER}")


def sts_provider() -> StsTokenPort:
    """Phase E5 — STS issuer for client-direct OSS uploads. Returns Fake in
    dev / test (no Aliyun SDK round-trip), real Aliyun provider in prod."""
    settings = get_settings()
    if settings.APP_ENV in ("dev", "test") or not settings.ALIYUN_STS_ROLE_ARN:
        from app.infrastructure.storage.fake_sts import FakeStsProvider

        return FakeStsProvider()
    from app.infrastructure.storage.aliyun_sts import AliyunStsProvider

    return AliyunStsProvider(settings)


def vision_service() -> FakeVision | QwenVision:
    settings = get_settings()
    if settings.VISION_PROVIDER == "fake":
        return FakeVision()
    if settings.VISION_PROVIDER == "qwen":
        return QwenVision(settings)
    raise ValueError(f"Unsupported vision provider: {settings.VISION_PROVIDER}")


def image_generation_service() -> FakeImageGeneration | WanxImageGeneration:
    # Reuse the vision provider switch: qwen → wanx image edit, else fake.
    settings = get_settings()
    if settings.VISION_PROVIDER == "qwen":
        return WanxImageGeneration(settings)
    return FakeImageGeneration()


def image_cutout_service() -> ImageCutoutPort:
    """V4 item cutout provider. ``fake`` (default, no key, returns the source
    unchanged) or ``openai`` (gpt-image-2 product-shot). Lazy-imported so the
    httpx adapter is only loaded when selected."""
    from app.infrastructure.vision.fake_image_cutout import FakeImageCutout
    from app.infrastructure.vision.openai_image_cutout import OpenAIImageCutout

    settings = get_settings()
    if settings.IMAGE_CUTOUT_PROVIDER == "openai":
        return OpenAIImageCutout(settings)
    return FakeImageCutout()


def ocr_service() -> OcrPort:
    """M18 — OCR provider for receipt / order-screenshot / shopping-email imports.

    Mirrors ``vision_service``: ``fake`` in dev/test (no key required), ``qwen``
    for the real Qwen-VL OCR endpoint. Lazy-imported so the optional providers
    are only loaded when selected.
    """
    from app.infrastructure.ocr.fake_ocr import FakeOcr
    from app.infrastructure.ocr.qwen_ocr import QwenOcr

    settings = get_settings()
    if settings.OCR_PROVIDER == "fake":
        return FakeOcr()
    if settings.OCR_PROVIDER == "qwen":
        return QwenOcr(settings)
    raise ValueError(f"Unsupported OCR provider: {settings.OCR_PROVIDER}")


def vision_rate_limiter() -> InMemoryVisionRateLimiter:
    global _vision_rate_limiter
    settings = get_settings()
    if _vision_rate_limiter is None:
        _vision_rate_limiter = InMemoryVisionRateLimiter(settings.VISION_MAX_CALLS_PER_USER_HOUR)
    return _vision_rate_limiter


def link_parser() -> RoutedLinkParser:
    return RoutedLinkParser(get_settings())


def weather_service() -> WeatherPort | None:
    settings = get_settings()
    if settings.WEATHER_PROVIDER == "none":
        return None
    if settings.WEATHER_PROVIDER == "qweather":
        return QWeatherProvider(settings, get_redis_client())
    if settings.WEATHER_PROVIDER == "openweather":
        return OpenWeatherProvider(settings, get_redis_client())
    raise ValueError(f"Unsupported weather provider: {settings.WEATHER_PROVIDER}")


def llm_service() -> LlmPort:
    from app.infrastructure.llm.openai_llm import NoOpLlm, OpenAiLlm
    from app.infrastructure.llm.qwen_llm import QwenLlm

    settings = get_settings()
    if settings.LLM_PROVIDER == "none":
        return NoOpLlm()
    if settings.LLM_PROVIDER == "openai":
        return OpenAiLlm(settings)
    if settings.LLM_PROVIDER == "qwen":
        return QwenLlm(settings)
    raise ValueError(f"Unsupported LLM provider: {settings.LLM_PROVIDER}")


def embedding_service() -> EmbeddingPort:
    from app.infrastructure.embedding.clip_embedder import ClipEmbedder
    from app.infrastructure.embedding.fake_embedding import FakeEmbedding
    from app.infrastructure.embedding.remote_clip_embedding import RemoteClipEmbedding

    settings = get_settings()
    if settings.EMBEDDING_PROVIDER == "fake":
        return FakeEmbedding()
    if settings.EMBEDDING_PROVIDER in {"clip", "open_clip"}:
        return ClipEmbedder(settings)
    if settings.EMBEDDING_PROVIDER == "remote_clip":
        return RemoteClipEmbedding(settings)
    raise ValueError(f"Unsupported embedding provider: {settings.EMBEDDING_PROVIDER}")


def push_provider() -> ApnsProvider:
    return ApnsProvider(get_settings())


async def arq_pool():  # type: ignore[no-untyped-def]
    """Lazy-initialized ARQ Redis pool for enqueuing jobs from endpoints.

    Returns None when arq is not installed.
    """
    if create_pool is None or RedisSettings is None:
        return None
    settings = get_settings()
    return await create_pool(_redis_settings_from_url(settings.ARQ_REDIS_URL))


def _redis_settings_from_url(url: str):  # type: ignore[no-untyped-def]
    if RedisSettings is None:
        return None
    parsed = urlparse(url)
    return RedisSettings(
        host=parsed.hostname or "localhost",
        port=parsed.port or 6379,
        database=int((parsed.path or "/0").lstrip("/") or "0"),
        password=parsed.password,
        ssl=parsed.scheme == "rediss",
    )
