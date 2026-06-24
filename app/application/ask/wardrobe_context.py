"""V4.1 Ask Wardrobe Context builder.

Assembles the structured, prompt-safe context that grounds an Ask answer —
a wardrobe summary, the user's style profile, fit preferences, recently worn
categories, departure time, and the ask scope. Used to (a) enrich the LLM
system prompt and (b) persist a `context_snapshot` on `ask_sessions.context`
for explainability / traceability.

Safety story (every field that reaches the prompt):
- `wardrobe_summary` / `recent_categories` — derived counts and category
  enums, no raw rows.
- `style_profile.preferred_colors` — from the whitelisted `color_main`.
- `style_profile.style_tags` — user-entered free text, but already bounded at
  the PATCH layer (<=20 tags x <=50 chars) and re-capped here.
- `fit_preferences` — enum values (fitted/regular/...) read from the persisted
  fit profile, not free text.
- `departure_time` — a HH:MM string.
No raw user_id, email, image_url, or free-form item titles reach the prompt;
the opaque `usr` token is attached separately by the prompt builder.
"""

from __future__ import annotations

import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import date
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.enums import ItemStatus
from app.infrastructure.persistence.models.user import UserSettings
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.recommendation_repo import (
    SqlAlchemyRecommendationRepository,
)
from app.infrastructure.persistence.repositories.style_repo import (
    SqlAlchemyStyleRepository,
)

# BE#27 — v2 adds `candidate_selection` (selection strategy + per-category
# counts + focused_in_top1 + prompt_field_schema_version) to the snapshot and
# makes `wardrobe_summary` count the full wearable set rather than the curated
# sample.
SNAPSHOT_VERSION = 2
_MAX_STYLE_TAGS = 20
_TOP_COLORS = 6


@dataclass(frozen=True)
class WardrobeContext:
    scope: str  # "item" | "wardrobe"
    focused_item_id: str | None
    locale: str  # resolved language subtag (e.g. "zh", "en")
    wardrobe_summary: dict[str, Any]
    style_profile: dict[str, Any]
    fit_preferences: dict[str, str]
    recent_categories: list[str]
    departure_time: str | None
    candidate_item_ids: list[str]
    candidate_selection: dict[str, Any]  # snapshot-only; never reaches the prompt


def summarize_wardrobe(items: list[WardrobeItem]) -> dict[str, Any]:
    by_category = Counter(i.category for i in items if i.category)
    return {"total": len(items), "by_category": dict(by_category)}


async def _wardrobe_summary(session: AsyncSession, user_id: uuid.UUID) -> dict[str, Any]:
    """Composition over the FULL wearable set, not the curated candidate sample.

    The summary tells the model the wardrobe's true makeup ("you have N tops"),
    so it must not undercount when candidates are deduped/capped. One GROUP BY,
    independent of the per-item prompt array."""
    rows = (
        await session.execute(
            select(WardrobeItem.category, func.count())
            .where(
                WardrobeItem.user_id == user_id,
                WardrobeItem.status == ItemStatus.WEARABLE.value,
                WardrobeItem.deleted_at.is_(None),
            )
            .group_by(WardrobeItem.category)
        )
    ).all()
    by_category = {category: count for category, count in rows if category}
    return {"total": sum(by_category.values()), "by_category": by_category}


def _top_colors(items: list[WardrobeItem]) -> list[str]:
    counts = Counter(i.color_main for i in items if i.color_main)
    return [color for color, _ in counts.most_common(_TOP_COLORS)]


async def build_wardrobe_context(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    items: list[WardrobeItem],
    focused_item_id: uuid.UUID | None,
    locale: str,
    today: date,
    candidate_selection: dict[str, Any] | None = None,
) -> WardrobeContext:
    style_tags = (await SqlAlchemyStyleRepository(session).get_style_tags(user_id))[
        :_MAX_STYLE_TAGS
    ]
    settings = await session.scalar(
        select(UserSettings).where(UserSettings.user_id == user_id)
    )
    departure_time = (
        settings.departure_time.strftime("%H:%M")
        if settings is not None and settings.departure_time is not None
        else None
    )
    fit_preferences = _fit_preferences(settings)
    recent_ids = await SqlAlchemyRecommendationRepository(
        session
    ).recent_worn_main_item_ids(user_id, today)
    recent_categories = sorted(
        {i.category for i in items if i.category and str(i.id) in recent_ids}
    )
    return WardrobeContext(
        scope="item" if focused_item_id else "wardrobe",
        focused_item_id=str(focused_item_id) if focused_item_id else None,
        locale=locale,
        wardrobe_summary=await _wardrobe_summary(session, user_id),
        style_profile={"style_tags": style_tags, "preferred_colors": _top_colors(items)},
        fit_preferences=fit_preferences,
        recent_categories=recent_categories,
        departure_time=departure_time,
        candidate_item_ids=[str(i.id) for i in items],
        candidate_selection=candidate_selection or {},
    )


def _fit_preferences(settings: UserSettings | None) -> dict[str, str]:
    # Reuse the persisted fit profile (cron-built, same source the recommender
    # trusts) instead of re-aggregating over history in the request path.
    if settings is None or not isinstance(settings.preferences_json, dict):
        return {}
    fit_profile = settings.preferences_json.get("fit_profile")
    if not isinstance(fit_profile, dict):
        return {}
    prefs = fit_profile.get("category_preferences")
    if not isinstance(prefs, dict):
        return {}
    return {str(k): str(v) for k, v in prefs.items() if isinstance(v, str)}


def context_for_prompt(ctx: WardrobeContext) -> dict[str, Any]:
    """The context block injected into the LLM prompt. Excludes
    candidate_item_ids — the items array carries per-item detail and the model
    cites by position, not id."""
    return {
        "scope": ctx.scope,
        "wardrobe_summary": ctx.wardrobe_summary,
        "style_profile": ctx.style_profile,
        "fit_preferences": ctx.fit_preferences,
        "recent_categories": ctx.recent_categories,
        "departure_time": ctx.departure_time,
    }


def context_snapshot(ctx: WardrobeContext) -> dict[str, Any]:
    """Versioned snapshot persisted (nested under "snapshot") on
    ask_sessions.context for explainability — the prompt context plus
    snapshot-only metadata (built from context_for_prompt so the two stay in
    sync when a dimension is added)."""
    return {
        **context_for_prompt(ctx),
        "snapshot_version": SNAPSHOT_VERSION,
        "focused_item_id": ctx.focused_item_id,
        "locale": ctx.locale,
        "candidate_item_ids": ctx.candidate_item_ids,
        "candidate_selection": ctx.candidate_selection,
    }
