"""BE#38/#85 — grounded recommendation reasoning.

Builds the structured `reasoning: [{type, text, source, referenced_item_ids,
confidence}]` layer for a Today outfit from REAL signals the engine already
computed (weather, freshness/recent-wear, color vs. learned style, category
balance, fit). Deterministic on purpose: it can only state facts derived from
the inputs, so it never invents "fake specificity" (BE#38's explicit warning)
the way a free-form LLM might. The LLM continues to phrase the short
`reason_text`; this layer is the trustworthy explanation.

Takes primitives (not EngineItem) to stay import-cycle-free with `engine`.

---
BE#85 — Source provenance + confidence rubric
=============================================
Internal type → FE source enum mapping:
  weather (cold+outerwear, warm, rain+dark-shoes) → weather_rule, high
  weather (neutral / temp-ok)                     → weather_rule, medium
  wear_history (freshness)                         → wardrobe_history, high
  style_match (learned palette overlap)            → preference_memory, medium
  category (dress / top+bottom)                    → style_memory, high
  fit                                              → wardrobe_history, high
  floor fillers (no real signal)                   → closest source, medium

Confidence rubric (applies here only — not the engine score):
  high   = rule-derived, one-to-one provenance (fact is true or not)
  medium = statistical / heuristic / secondary inference
  low    = LLM-authored (not emitted by this deterministic layer)

referenced_item_ids: list of item UUIDs that directly drove the entry.
  Only attached when the reason is about specific item(s). Never padded.
  Whole-look reasons (e.g., category balance) → [].

Sources NOT emitted by this layer:
  llm                     — reason_text is LLM-authored; this layer is not

Sources emitted here:
  indoor_climate_inference — BE#82, medium confidence, only when a tendency was
    inferred AND the look carries a removable outer layer (never labels the user)
  feedback_learning — the user's own "too warm" / "too cold" feedback changed
    the layering of this look; high confidence (it is their stated reason)
"""

from __future__ import annotations

from typing import Any

from app.application.ports.weather import WeatherInfo
from app.application.recommendations.indoor_climate import INDOOR_COLD
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
INDOOR_CLIMATE = "indoor_climate"
FEEDBACK = "feedback"


def _entry(
    type_: str,
    text: str,
    source: str,
    confidence: str,
    referenced_item_ids: list[str] | None = None,
) -> dict[str, Any]:
    """Construct a single reasoning entry with all BE#85 provenance fields."""
    return {
        "type": type_,
        "text": text,
        "source": source,
        "confidence": confidence,
        "referenced_item_ids": referenced_item_ids or [],
    }


def build_reasoning(
    *,
    categories: list[str],
    colors: list[str],
    fit_statuses: list[str],
    is_fresh: bool,
    weather: WeatherInfo | None,
    preferred_colors: set[str] | None = None,
    item_ids: list[str] | None = None,
    main_item_id: str | None = None,
    indoor_climate: str | None = None,
    thermal_offset_c: float = 0.0,
    max_items: int = 4,
) -> list[dict[str, Any]]:
    """Return 2–4 grounded reasons (Chinese) with source provenance (BE#85).

    Each entry: {type, text, source, referenced_item_ids, confidence}.
    Order = priority; weather first when present. Every item is backed by an
    input fact — no fabrication.

    item_ids and categories/colors/fit_statuses are parallel lists (same index
    = same item). main_item_id is the freshness-relevant item (top or dress).
    Both default to None for callers that pre-date BE#85.
    """
    cats = set(categories)
    reasons: list[dict[str, Any]] = []
    ids = item_ids or []

    # Build per-category item-id index for targeted referenced_item_ids.
    shoe_ids = [
        ids[i]
        for i, cat in enumerate(categories)
        if cat == Category.SHOES.value and i < len(ids)
    ]
    outerwear_ids = [
        ids[i]
        for i, cat in enumerate(categories)
        if cat == Category.OUTERWEAR.value and i < len(ids)
    ]
    fits_well_ids = [
        ids[i]
        for i, status in enumerate(fit_statuses)
        if status == "fits_well" and i < len(ids)
    ]

    # 0. The user's own comfort feedback, when it shaped this look's layering.
    #    First, so "you told us X, so we did Y" is the reason people see.
    if weather is not None and thermal_offset_c:
        has_outer = Category.OUTERWEAR.value in cats
        if thermal_offset_c > 0 and not has_outer:
            reasons.append(_entry(
                FEEDBACK,
                "你上次说太热，这次没加外层",
                source="feedback_learning",
                confidence="high",
            ))
        elif thermal_offset_c < 0 and has_outer:
            reasons.append(_entry(
                FEEDBACK,
                "你上次说偏冷，这次加了外层",
                source="feedback_learning",
                confidence="high",
                referenced_item_ids=outerwear_ids,
            ))

    # 1. Weather — only claims that match the actual outfit + forecast.
    if weather is not None:
        has_outer = Category.OUTERWEAR.value in cats
        if weather.temperature_c < _COLD_C and has_outer:
            # Rule: cold + outerwear present → warmth claim, cite the outerwear.
            reasons.append(_entry(
                WEATHER,
                "今天气温偏低，这件外层能压住寒意",
                source="weather_rule",
                confidence="high",
                referenced_item_ids=outerwear_ids,
            ))
        elif weather.temperature_c >= _WARM_C:
            # Rule: warm day, whole-look claim — no specific item.
            reasons.append(_entry(
                WEATHER,
                "今天偏暖，这身轻薄透气更舒服",
                source="weather_rule",
                confidence="high",
            ))
        elif weather.temperature_c >= _COLD_C:
            # Heuristic: neutral temperature — moderate confidence.
            reasons.append(_entry(
                WEATHER,
                "今天气温适中，整体好驾驭",
                source="weather_rule",
                confidence="medium",
            ))
        shoe_colors = [
            col.lower()
            for cat, col in zip(categories, colors, strict=False)
            if cat == Category.SHOES.value
        ]
        if (
            weather.rain_probability > _RAIN_PROB
            and shoe_colors
            and not any(c in _LIGHT_SHOE_COLORS for c in shoe_colors)
        ):
            # Only when the outfit actually has dark shoes — don't invent a shoe
            # claim for a shoeless look. Cite the specific shoe item(s).
            reasons.append(_entry(
                WEATHER,
                "今天可能有雨，这双深色鞋面更耐脏好打理",
                source="weather_rule",
                confidence="high",
                referenced_item_ids=shoe_ids,
            ))

    # 1b. Indoor climate (BE#82) — only when a tendency was inferred AND the look
    # carries a removable outer layer to act on. Copy never labels the user by
    # commute type (privacy requirement); it speaks to the removable layer.
    if indoor_climate is not None and Category.OUTERWEAR.value in cats:
        text = (
            "室内外温差大，带上这件可脱的外层，室内偏凉时正好加一件"
            if indoor_climate == INDOOR_COLD
            else "室内容易偏热，这件外层能随手脱掉，方便调节"
        )
        reasons.append(_entry(
            INDOOR_CLIMATE,
            text,
            source="indoor_climate_inference",
            confidence="medium",
            referenced_item_ids=outerwear_ids,
        ))

    # 2. Freshness — the main piece hasn't been worn recently.
    if is_fresh:
        reasons.append(_entry(
            WEAR_HISTORY,
            "这件你最近没怎么穿，换个新鲜感",
            source="wardrobe_history",
            confidence="high",
            referenced_item_ids=[main_item_id] if main_item_id else [],
        ))

    # 3. Style match — colors overlap the user's learned palette.
    if preferred_colors:
        overlap = [c for c in colors if c in preferred_colors]
        if overlap:
            # Statistical: learned from wear history, not a hard rule.
            reasons.append(_entry(
                STYLE_MATCH,
                "配色贴合你平时常穿的风格",
                source="preference_memory",
                confidence="medium",
            ))

    # 4. Category balance — a complete, sensible silhouette.
    if Category.DRESS.value in cats:
        reasons.append(_entry(
            CATEGORY,
            "一条连衣裙省心又利落",
            source="style_memory",
            confidence="high",
        ))
    elif Category.TOP.value in cats and Category.BOTTOM.value in cats:
        reasons.append(_entry(
            CATEGORY,
            "上下身比例均衡，是稳妥的组合",
            source="style_memory",
            confidence="high",
        ))

    # 5. Fit — phrased for the confirmed piece(s) only; never claim the whole
    # outfit fits when only some items are confirmed (the rest are unknown).
    # Source: wardrobe_history (closest — based on wear + fit log, not a
    # separate enum slot). Confidence high: per-item fit status is factual.
    if all(status == "fits_well" for status in fit_statuses) and fit_statuses:
        reasons.append(_entry(
            FIT,
            "这些单品你穿着都合身",
            source="wardrobe_history",
            confidence="high",
            referenced_item_ids=fits_well_ids,
        ))
    elif any(status == "fits_well" for status in fit_statuses):
        reasons.append(_entry(
            FIT,
            "其中有确认合身的单品",
            source="wardrobe_history",
            confidence="high",
            referenced_item_ids=fits_well_ids,
        ))

    # Floor: always give at least two grounded reasons. The category/color facts
    # below are true of any non-empty combo, so they never fabricate.
    # These are floor fillers — medium confidence (no strong signal, just facts).
    if len(reasons) < 2 and colors:
        reasons.append(_entry(
            STYLE_MATCH,
            f"以 {colors[0]} 为主色，整体协调",
            source="preference_memory",
            confidence="medium",
        ))
    if len(reasons) < 2:
        reasons.append(_entry(
            CATEGORY,
            "根据你的衣橱为今天挑选",
            source="style_memory",
            confidence="medium",
        ))

    # De-dup by text (weather can add two; keep order) and cap.
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for reason in reasons:
        if reason["text"] not in seen:
            seen.add(reason["text"])
            deduped.append(reason)
    return deduped[:max_items]
