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


@dataclass(frozen=True)
class StyleAnalysis:
    """Result of analyzing a fashion-inspiration image for its aesthetic.

    `style_tags` are lowercase English style descriptors (e.g. minimal, formal,
    street) — the field the recommendation `style_context` template matcher and
    `build_style_profile` consume. `color_palette` is the dominant colors; both
    are folded into the persisted `style_tags` because the template rules key on
    color words too. `avoid_tags` is captured for transparency / future use.
    """

    style_tags: list[str] = field(default_factory=list)
    color_palette: list[str] = field(default_factory=list)
    avoid_tags: list[str] = field(default_factory=list)
    confidence: float = 0.0


class VisionPort(Protocol):
    async def analyze_image(self, image_url: str, locale: str = DEFAULT_LOCALE) -> VisionResult: ...

    async def analyze_style(self, image_url: str, locale: str = DEFAULT_LOCALE) -> StyleAnalysis: ...
