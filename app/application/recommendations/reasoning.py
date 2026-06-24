"""BE#38 — grounded recommendation reasoning.

Builds the structured `reasoning: [{type, text}]` layer for a Today outfit from
REAL signals the engine already computed (weather, freshness/recent-wear, color
vs. learned style, category balance, fit). Deterministic on purpose: it can only
state facts derived from the inputs, so it never invents "fake specificity"
(BE#38's explicit warning) the way a free-form LLM might. The LLM continues to
phrase the short `reason_text`; this layer is the trustworthy explanation.

Takes primitives (not EngineItem) to stay import-cycle-free with `engine`.
"""

from __future__ import annotations

from app.application.ports.weather import WeatherInfo
from app.common.enums import Category

_COLD_C = 10.0
_WARM_C = 22.0
_RAIN_PROB = 0.5
_LIGHT_SHOE_COLORS = {"white", "ivory", "cream", "beige", "light gray", "light grey", "silver"}

# Reasoning item `type`s (FE renders/keys off these).
WEATHER = "weather"
WEAR_HISTORY = "wear_history"
STYLE_MATCH = "style_match"
CATEGORY = "category"
FIT = "fit"


def build_reasoning(
    *,
    categories: list[str],
    colors: list[str],
    fit_statuses: list[str],
    is_fresh: bool,
    weather: WeatherInfo | None,
    preferred_colors: set[str] | None = None,
    max_items: int = 4,
) -> list[dict[str, str]]:
    """Return 2–4 grounded {type, text} reasons (Chinese). Order = priority;
    weather first when present. Every item is backed by an input fact."""
    cats = set(categories)
    reasons: list[dict[str, str]] = []

    # 1. Weather — only claims that match the actual outfit + forecast.
    if weather is not None:
        has_outer = Category.OUTERWEAR.value in cats
        if weather.temperature_c < _COLD_C and has_outer:
            reasons.append({"type": WEATHER, "text": "今天气温偏低，这件外层能压住寒意"})
        elif weather.temperature_c >= _WARM_C:
            reasons.append({"type": WEATHER, "text": "今天偏暖，这身轻薄透气更舒服"})
        elif weather.temperature_c >= _COLD_C:
            reasons.append({"type": WEATHER, "text": "今天气温适中，整体好驾驭"})
        if weather.rain_probability > _RAIN_PROB and not any(
            c.lower() in _LIGHT_SHOE_COLORS for c in colors
        ):
            reasons.append({"type": WEATHER, "text": "今天可能有雨，鞋面更耐脏好打理"})

    # 2. Freshness — the main piece hasn't been worn recently.
    if is_fresh:
        reasons.append({"type": WEAR_HISTORY, "text": "这件你最近没怎么穿，换个新鲜感"})

    # 3. Style match — colors overlap the user's learned palette.
    if preferred_colors:
        overlap = [c for c in colors if c in preferred_colors]
        if overlap:
            reasons.append({"type": STYLE_MATCH, "text": "配色贴合你平时常穿的风格"})

    # 4. Category balance — a complete, sensible silhouette.
    if Category.DRESS.value in cats:
        reasons.append({"type": CATEGORY, "text": "一条连衣裙省心又利落"})
    elif Category.TOP.value in cats and Category.BOTTOM.value in cats:
        reasons.append({"type": CATEGORY, "text": "上下身比例均衡，是稳妥的组合"})

    # 5. Fit — at least one piece is confirmed to fit well.
    if any(status == "fits_well" for status in fit_statuses):
        reasons.append({"type": FIT, "text": "这些单品你穿着都合身"})

    # Floor: always give at least two grounded reasons. The category/color facts
    # below are true of any non-empty combo, so they never fabricate.
    if len(reasons) < 2 and colors:
        reasons.append({"type": STYLE_MATCH, "text": f"以 {colors[0]} 为主色，整体协调"})
    if len(reasons) < 2:
        reasons.append({"type": CATEGORY, "text": "根据你的衣橱为今天挑选"})

    # De-dup by text (weather can add two; keep order) and cap.
    seen: set[str] = set()
    deduped: list[dict[str, str]] = []
    for reason in reasons:
        if reason["text"] not in seen:
            seen.add(reason["text"])
            deduped.append(reason)
    return deduped[:max_items]
