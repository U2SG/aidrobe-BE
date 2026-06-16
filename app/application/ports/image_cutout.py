from typing import Protocol


class ImageCutoutPort(Protocol):
    """Turn a source photo into a clean product-shot cutout of a single item.

    Implementations are GENERATIVE (gpt-image-2 redraws onto a white
    background), so the output is a derived *display* image — close to the
    real garment but not a pixel-faithful crop. Callers persist the original
    separately and treat the cutout as the cover image only."""

    async def extract(self, image_bytes: bytes, category: str) -> bytes:
        """Return cutout image bytes (PNG/JPEG). Raises on provider failure so
        the worker can retry / dead-letter."""
        ...
