"""Use an owned, genuinely analysed inspiration for the current design only."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from math import isfinite

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common import error_codes
from app.common.exceptions import FriendlyException
from app.config.settings import Settings
from app.infrastructure.llm.sanitizer import sanitize_llm_text
from app.infrastructure.persistence.models.style import UserStyleProfile


def _terms(value: object, limit: int) -> list[str]:
    if not isinstance(value, list):
        return []
    terms = [sanitize_llm_text(term, max_len=50).strip() for term in value if isinstance(term, str)]
    return list(dict.fromkeys(term for term in terms if term))[:limit]


@dataclass(frozen=True)
class DesignReference:
    styles: list[str]
    palette: list[str]
    confidence: float

    def prompt_context(self) -> str:
        return (
            "\nSelected inspiration's image analysis: "
            + json.dumps({"style_tags": self.styles, "color_palette": self.palette,
                          "confidence": self.confidence}, ensure_ascii=False)
            + "\nThis is visual evidence for this design, not owned clothing or a durable preference. "
            "Use actual wardrobe pieces to interpret it, keep the user's explicit conditions, and explain gaps. "
            "Only style and palette were analysed; do not claim to know cut, fit, fabric or comfort from these tags."
        )


def reference_from_profile(profile: UserStyleProfile, settings: Settings) -> DesignReference | None:
    if not (settings.ENABLE_DESIGNER_SESSIONS and settings.ENABLE_VISION_STYLE_LEARNING):
        return None
    summary = profile.learned_summary or {}
    if (profile.source_type != "inspiration_image" or profile.learn_status != "learned"
            or not profile.image_url or summary.get("source") != "vision"):
        return None
    styles = _terms(summary.get("style_tags"), 8)
    if not styles:
        return None
    value = summary.get("confidence")
    confidence = float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 0.0
    if not isfinite(confidence):
        confidence = 0.0
    return DesignReference(styles, _terms(summary.get("color_palette"), 5), min(1.0, max(0.0, confidence)))


async def load_design_reference(
    session: AsyncSession, user_id: uuid.UUID, reference_id: uuid.UUID, *, settings: Settings, strict: bool,
) -> DesignReference | None:
    if not (settings.ENABLE_DESIGNER_SESSIONS and settings.ENABLE_VISION_STYLE_LEARNING):
        if strict:
            raise FriendlyException(error_codes.FORBIDDEN, "Design references are not enabled", 503)
        return None
    profile = await session.scalar(select(UserStyleProfile).where(
        UserStyleProfile.id == reference_id, UserStyleProfile.user_id == user_id,
    ).execution_options(populate_existing=True))
    if profile is None:
        if strict:
            raise FriendlyException(error_codes.NOT_FOUND, "Inspiration not found", 404)
        return None
    reference = reference_from_profile(profile, settings)
    if reference is None and strict:
        raise FriendlyException(error_codes.STATE_CONFLICT, "This inspiration has no ready image analysis", 409)
    return reference
