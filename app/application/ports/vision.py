from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class VisionResult:
    title: str
    category: str
    color_main: str
    confidence_score: float


class VisionPort(Protocol):
    async def analyze_image(self, image_url: str) -> VisionResult: ...
