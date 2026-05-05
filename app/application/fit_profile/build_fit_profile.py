import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.persistence.models.fit import FitFeedback
from app.infrastructure.persistence.models.user import User, UserSettings
from app.infrastructure.persistence.models.wardrobe import WardrobeItem

logger = structlog.get_logger(__name__)

LOOSE_SIGNALS = {"loose", "easy_to_move"}
TIGHT_SIGNALS = {"tight", "uncomfortable"}
MIN_FEEDBACK_FOR_PREFERENCE = 3

_POSITIVE_TAG_SIGNALS = {
    "fits_well": 1.0,
    "easy_to_move": 0.8,
}
_NEGATIVE_TAG_SIGNALS = {
    "tight": -1.0,
    "uncomfortable": -0.8,
    "not_flattering": -0.7,
}
_LOOSE_FIT_INTENTS = {"relaxed", "oversized", "wide_leg"}


@dataclass(frozen=True)
class FitFeedbackSignal:
    feedback_type: str
    category: str
    fit_intent: str | None


async def build_fit_profile(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    persist: bool = True,
) -> dict[str, Any]:
    signals = await _load_feedback_signals(session, user_id)
    category_preferences = _compute_category_preferences(signals)
    body_shape_tag_weights = _compute_body_shape_tag_weights(signals)

    profile: dict[str, Any] = {
        "version": 1,
        "category_preferences": category_preferences,
        "body_shape_tag_weights": body_shape_tag_weights,
        "feedback_count": len(signals),
        "updated_at": datetime.now(UTC).isoformat(),
    }
    if persist:
        await _write_user_fit_profile(session, user_id, profile)

    logger.info(
        "fit_profile_built",
        user_id=str(user_id),
        feedback_count=len(signals),
        category_count=len(category_preferences),
        tag_count=len(body_shape_tag_weights),
    )
    return profile


async def aggregate_fit_profile(session: AsyncSession, user_id: uuid.UUID) -> dict[str, str]:
    profile = await build_fit_profile(session, user_id, persist=False)
    category_preferences = profile.get("category_preferences")
    if not isinstance(category_preferences, dict):
        return {}
    return {
        str(category): str(preference)
        for category, preference in category_preferences.items()
        if isinstance(preference, str)
    }


async def build_all_fit_profiles(session: AsyncSession) -> int:
    result = await session.scalars(select(FitFeedback.user_id).distinct())
    user_ids = list(result)
    for user_id in user_ids:
        await build_fit_profile(session, user_id)
    return len(user_ids)


async def _load_feedback_signals(
    session: AsyncSession,
    user_id: uuid.UUID,
) -> list[FitFeedbackSignal]:
    rows = await session.execute(
        select(FitFeedback.feedback_type, WardrobeItem.category, WardrobeItem.fit_intent)
        .join(WardrobeItem, FitFeedback.item_id == WardrobeItem.id, isouter=True)
        .where(FitFeedback.user_id == user_id)
        .order_by(FitFeedback.created_at.desc())
    )
    return [
        FitFeedbackSignal(
            feedback_type=str(feedback_type),
            category=str(category or "unknown"),
            fit_intent=str(fit_intent) if fit_intent else None,
        )
        for feedback_type, category, fit_intent in rows.all()
    ]


def _compute_category_preferences(signals: list[FitFeedbackSignal]) -> dict[str, str]:
    category_tallies: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for signal in signals:
        category_tallies[signal.category][signal.feedback_type] += 1

    profile: dict[str, str] = {}
    for category, tallies in category_tallies.items():
        loose_count = sum(tallies.get(signal, 0) for signal in LOOSE_SIGNALS)
        tight_count = sum(tallies.get(signal, 0) for signal in TIGHT_SIGNALS)
        total = loose_count + tight_count

        if total < MIN_FEEDBACK_FOR_PREFERENCE:
            continue

        loose_ratio = loose_count / total
        tight_ratio = tight_count / total
        if loose_ratio >= 0.6:
            profile[category] = "prefers_loose"
        elif tight_ratio >= 0.6:
            profile[category] = "prefers_fitted"
        else:
            profile[category] = "balanced"
    return profile


def _compute_body_shape_tag_weights(signals: list[FitFeedbackSignal]) -> dict[str, float]:
    totals: dict[str, float] = defaultdict(float)
    counts: dict[str, int] = defaultdict(int)

    for signal in signals:
        if signal.fit_intent is None:
            continue
        weight = _signal_weight(signal.feedback_type, signal.fit_intent)
        if weight == 0.0:
            continue

        for tag in (signal.fit_intent, f"{signal.category}:{signal.fit_intent}"):
            totals[tag] += weight
            counts[tag] += 1

    return {
        tag: round(max(-1.0, min(1.0, totals[tag] / counts[tag])), 4)
        for tag in sorted(totals)
        if counts[tag] > 0
    }


def _signal_weight(feedback_type: str, fit_intent: str) -> float:
    if feedback_type in _POSITIVE_TAG_SIGNALS:
        return _POSITIVE_TAG_SIGNALS[feedback_type]
    if feedback_type in _NEGATIVE_TAG_SIGNALS:
        return _NEGATIVE_TAG_SIGNALS[feedback_type]
    if feedback_type == "loose":
        return 0.6 if fit_intent in _LOOSE_FIT_INTENTS else -0.2
    return 0.0


async def _write_user_fit_profile(
    session: AsyncSession,
    user_id: uuid.UUID,
    profile: dict[str, Any],
) -> None:
    user_exists = await session.scalar(select(User.id).where(User.id == user_id))
    if user_exists is None:
        return

    settings = await session.scalar(select(UserSettings).where(UserSettings.user_id == user_id))
    if settings is None:
        settings = UserSettings(user_id=user_id, preferences_json={})
        session.add(settings)
        await session.flush()

    preferences = dict(settings.preferences_json or {})
    preferences["fit_profile"] = profile
    settings.preferences_json = preferences
    await session.flush()
