from dataclasses import dataclass, field
from typing import Protocol


@dataclass
class ImageProcessingResult:
    normalized_url: str
    thumbnail_url: str
    cutout_url: str
    quality_score: float
    quality_flags: list[str] = field(default_factory=list)


class ImageProcessorPort(Protocol):
    async def process(self, image_url: str, item_id: str, user_id: str) -> ImageProcessingResult:
        ...
