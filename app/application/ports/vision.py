from dataclasses import dataclass, field
from typing import Protocol

from app.common.locale import DEFAULT_LOCALE


@dataclass(frozen=True)
class VisionResult:
    # BE#30 — naming is now locale-aware.
    # - `canonical_name`: stable English display name (always English).
    # - `display_name`: name in the requested locale (zh-CN -> Simplified Chinese).
    # - `localized_names`: {locale: name} map for every locale produced.
    # - `title`: kept for backward compatibility; mirrors `display_name`.
    # `category` / `color_main` stay stable English enum-like fields.
    title: str
    category: str
    color_main: str
    confidence_score: float
    canonical_name: str = ""
    display_name: str = ""
    localized_names: dict[str, str] = field(default_factory=dict)


class VisionPort(Protocol):
    async def analyze_image(self, image_url: str, locale: str = DEFAULT_LOCALE) -> VisionResult: ...
