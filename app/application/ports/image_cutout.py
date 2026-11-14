from dataclasses import dataclass
from typing import Literal, Protocol

CutoutOutcome = Literal["succeeded", "quality_rejected", "not_called"]


@dataclass(frozen=True)
class CutoutResult:
    outcome: CutoutOutcome
    image: bytes | None = None
    provider_cost_cents: int | None = None


class ImageCutoutPort(Protocol):
    """Turn a source photo into a clean product-shot cutout of a single item.

    Implementations are GENERATIVE (gpt-image-2 redraws onto a white
    background), so the output is a derived *display* image — close to the
    real garment but not a pixel-faithful crop. Callers persist the original
    separately and treat the cutout as the cover image only."""

    async def extract(
        self, image_bytes: bytes, category: str, *, title: str | None = None, color: str | None = None
    ) -> CutoutResult:
        """Return the result of generation and review. Provider errors raise."""
        ...
