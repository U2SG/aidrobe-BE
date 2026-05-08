from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class ImageProcessingResult:
    normalized_url: str
    thumbnail_url: str
    cutout_url: str
    quality_score: float
    quality_flags: list[str] = field(default_factory=list)
    # Phase E5 — stable object keys alongside the URLs. Writers persist
    # these to the new `object_key` column so reads can sign URLs at
    # request time instead of baking in TTL-bound URLs at write time.
    # Optional because legacy / pil_unavailable fallback paths may not
    # have a backing object.
    normalized_key: str | None = None
    thumbnail_key: str | None = None
    cutout_key: str | None = None


class ImageProcessorPort(Protocol):
    async def process(self, image_url: str, item_id: str, user_id: str) -> ImageProcessingResult:
        ...
