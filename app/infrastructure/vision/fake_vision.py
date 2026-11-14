from dataclasses import dataclass, replace

from app.application.ports.vision import StyleAnalysis, VisionResult
from app.common.locale import DEFAULT_LOCALE, resolve_locale


@dataclass(frozen=True)
class _FakeItem:
    category: str
    color_main: str
    confidence_score: float
    canonical_name: str
    # Canonical (English) + zh-CN display names, keyed by locale. Lets the
    # locale acceptance tests assert a Chinese display_name for zh-CN and an
    # English one for en-US while category/color stay English.
    names: dict[str, str]
    formality_score: int | None = None
    season_tags: tuple[str, ...] = ()
    material: str | None = None
    style_tags: tuple[str, ...] = ()


_ITEMS: dict[str, _FakeItem] = {
    "bottom": _FakeItem(
        category="bottom",
        color_main="black",
        confidence_score=0.85,
        canonical_name="Black trousers",
        names={"en-US": "Black trousers", "zh-CN": "黑色长裤"},
        formality_score=70,
        season_tags=("all_season",),
        material="wool",
        style_tags=("tailored", "classic"),
    ),
    "top": _FakeItem(
        category="top",
        color_main="white",
        confidence_score=0.9,
        canonical_name="White shirt",
        names={"en-US": "White shirt", "zh-CN": "白色衬衫"},
        formality_score=50,
        season_tags=("spring", "summer", "autumn"),
        material="cotton",
        style_tags=("minimal", "classic"),
    ),
}


def _result_for(key: str, locale: str) -> VisionResult:
    spec = _ITEMS[key]
    resolved = resolve_locale(locale)
    display_name = spec.names.get(resolved, spec.canonical_name)
    return VisionResult(
        title=display_name,
        category=spec.category,
        color_main=spec.color_main,
        confidence_score=spec.confidence_score,
        canonical_name=spec.canonical_name,
        display_name=display_name,
        localized_names=dict(spec.names),
        formality_score=spec.formality_score,
        season_tags=list(spec.season_tags),
        material=spec.material,
        style_tags=list(spec.style_tags),
    )


class FakeVision:
    async def analyze_image(
        self, image_url: str, locale: str = DEFAULT_LOCALE, *, source_key: str | None = None
    ) -> VisionResult:
        if "fail" in image_url:
            raise ValueError("fake vision failure")
        key = "bottom" if ("pants" in image_url or "bottom" in image_url) else "top"
        return _result_for(key, locale)

    async def analyze_outfit(self, image_url: str, locale: str = DEFAULT_LOCALE) -> list[VisionResult]:
        if "fail" in image_url:
            raise ValueError("fake vision failure")
        # Upper and lower halves of the photo, as a model would box them.
        return [
            replace(_result_for("top", locale), bbox=(0.2, 0.08, 0.8, 0.52)),
            replace(_result_for("bottom", locale), bbox=(0.25, 0.48, 0.75, 0.95)),
        ]

    async def analyze_style(self, image_url: str, locale: str = DEFAULT_LOCALE) -> StyleAnalysis:
        if "fail" in image_url:
            raise ValueError("fake vision failure")
        # Deterministic style by URL keyword so tests can assert the wire-through
        # (and that learned tags drive template selection in style_context).
        if "feminine" in image_url:
            return StyleAnalysis(
                style_tags=["feminine", "soft", "floral"],
                color_palette=["pink", "white"],
                avoid_tags=["edgy"],
                confidence=0.8,
            )
        return StyleAnalysis(
            style_tags=["minimal", "clean", "tailored"],
            color_palette=["black", "white"],
            avoid_tags=[],
            confidence=0.85,
        )
