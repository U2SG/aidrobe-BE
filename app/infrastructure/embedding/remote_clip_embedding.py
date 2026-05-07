import httpx
import structlog

from app.config.settings import Settings

logger = structlog.get_logger(__name__)


class RemoteClipEmbedding:
    """HTTP-based CLIP image embedding service.

    Posts image_url to a remote inference endpoint that returns a 512-dim
    CLIP embedding vector. The endpoint is expected to accept
    `{"image_url": "..."}` and return `{"embedding": [0.1, 0.2, ...]}`.
    """

    DIM = 512
    model_version = "remote_clip:512"

    def __init__(self, settings: Settings) -> None:
        if not settings.CLIP_EMBEDDING_API_KEY:
            raise ValueError("CLIP_EMBEDDING_API_KEY is required when EMBEDDING_PROVIDER=remote_clip")
        self.settings = settings

    async def embed_image(self, image_url: str) -> list[float]:
        payload = {"image_url": image_url}
        headers = {
            "Authorization": f"Bearer {self.settings.CLIP_EMBEDDING_API_KEY}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(timeout=self.settings.CLIP_EMBEDDING_TIMEOUT_SECONDS) as client:
            response = await client.post(
                self.settings.CLIP_EMBEDDING_BASE_URL,
                headers=headers,
                json=payload,
            )
            response.raise_for_status()
            data = response.json()
        embedding: list[float] = data["embedding"]
        if len(embedding) != self.DIM:
            logger.warning(
                "clip_embedding_dimension_mismatch",
                expected=self.DIM,
                actual=len(embedding),
            )
            # Pad or truncate
            if len(embedding) < self.DIM:
                embedding += [0.0] * (self.DIM - len(embedding))
            else:
                embedding = embedding[: self.DIM]
        return embedding
