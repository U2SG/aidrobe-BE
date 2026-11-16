"""Analyze a saved style image using the same bounded vision path as upload."""

import uuid
from datetime import UTC, datetime
from typing import Any

import structlog

from app.config.settings import Settings
from app.infrastructure.persistence.models.style import UserStyleProfile
from app.infrastructure.storage.source_image import resolve_import_source_key

logger = structlog.get_logger(__name__)
_MAX_LEARNED_TAGS = 10


async def learn_from_image(
    image_url: str, user_id: uuid.UUID, profile_id: str, settings: Settings
) -> dict[str, Any] | None:
    """Run real visual style analysis (Qwen-VL) over an inspiration image.

    Returns a ``learned_summary`` dict (with a combined ``tags`` list ready for
    the consumed ``style_tags`` column), or ``None`` to signal the caller should
    fall back to the URL-domain heuristic. Fail-soft: any vision error returns
    ``None`` so a user's inspiration always finishes "learning".
    """
    if not (settings.ENABLE_VISION_STYLE_LEARNING and image_url):
        return None
    # A real vision provider must be configured. If the flag is flipped on while
    # VISION_PROVIDER is still "fake" (e.g. a staging flip before Qwen is wired),
    # the container would hand back FakeVision — never persist its deterministic
    # output as learned style; degrade to the URL heuristic instead.
    if settings.VISION_PROVIDER == "fake":
        return None
    from app.bootstrap.container import vision_rate_limiter, vision_service

    try:
        # Same per-user hourly budget the wardrobe vision path enforces, so a
        # /style/sources/batch (up to 20 URLs) can't bypass it. Over budget →
        # degrade to the heuristic rather than failing the inspiration.
        vision_rate_limiter().check(user_id)
        source_key = resolve_import_source_key(image_url, user_id, settings)
        analysis = await vision_service().analyze_style(image_url, source_key=source_key)
    except Exception as exc:
        logger.warning("style_learn_vision_failed", profile_id=profile_id, reason=type(exc).__name__)
        return None

    # The template matcher (style_context._TEMPLATE_RULES) keys on both style
    # descriptors and color words, so persist them combined (deduped, capped).
    combined: list[str] = []
    for tag in [*analysis.style_tags, *analysis.color_palette]:
        if tag not in combined:
            combined.append(tag)
    if not combined:
        return None
    return {
        "tags": combined[:_MAX_LEARNED_TAGS],
        "style_tags": analysis.style_tags,
        "color_palette": analysis.color_palette,
        "avoid_tags": analysis.avoid_tags,
        "confidence": analysis.confidence,
        "source": "vision",
    }


def apply_visual_style(profile: UserStyleProfile, analysis: dict[str, Any]) -> None:
    profile.style_tags = {"tags": analysis["tags"]}
    profile.learned_summary = analysis
    profile.learn_status = "learned"
    profile.learned_at = datetime.now(UTC)
