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
    # Attributes the recommender ranks on. All optional: absent means unknown
    # (never "neutral"), and readers must not penalize unknown values.
    # - formality_score: 0..100 (the WardrobeItem / scoring scale)
    # - season_tags: subset of spring/summer/autumn/winter, or ["all_season"]
    # - material: one lowercase English word
    # - style_tags: lowercase English descriptors
    formality_score: int | None = None
    season_tags: list[str] = field(default_factory=list)
    material: str | None = None
    style_tags: list[str] = field(default_factory=list)
    # Where the garment is in the photo, as fractions of width/height:
    # (x_min, y_min, x_max, y_max). Only for multi-garment photos; None when
    # the model gave no usable box (the candidate then keeps the full photo).
    bbox: tuple[float, float, float, float] | None = None
    # Outfit recognition can see a layer without enough evidence to name its style.
    style_unverified: bool = False


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

    async def analyze_outfit(self, image_url: str, locale: str = DEFAULT_LOCALE) -> list[VisionResult]: ...

    async def review_garment_crops(self, crops: list[bytes], garments: list[VisionResult]) -> list[bool]: ...

    async def analyze_style(self, image_url: str, locale: str = DEFAULT_LOCALE) -> StyleAnalysis: ...
