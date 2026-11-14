import uuid
from datetime import UTC, datetime
from typing import Any

import structlog

from app.config.settings import Settings, get_settings
from app.infrastructure.persistence.session import AsyncSessionLocal
from app.infrastructure.storage.source_image import resolve_import_source_key

logger = structlog.get_logger(__name__)

# Cap the combined (style + color) tag list persisted into style_tags.
_MAX_LEARNED_TAGS = 10

_DOMAIN_STYLE_TAGS: list[tuple[str, list[str]]] = [
    ("xiaohongshu", ["lifestyle", "fashion", "korean"]),
    ("xhslink", ["lifestyle", "fashion", "korean"]),
    ("douyin", ["trendy", "streetwear"]),
    ("tiktok", ["trendy", "streetwear"]),
    ("taobao", ["casual", "chinese_fashion"]),
    ("tmall", ["casual", "chinese_fashion"]),
    ("pinterest", ["aesthetic", "inspiration"]),
    ("instagram", ["fashion", "lifestyle"]),
]


def _infer_style_tags(source_url: str | None) -> list[str]:
    if not source_url:
        return ["general"]
    url_lower = source_url.lower()
    for domain, tags in _DOMAIN_STYLE_TAGS:
        if domain in url_lower:
            return tags
    return ["general"]


async def _learn_from_image(
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
        logger.warning("style_learn_vision_failed", profile_id=profile_id, error=str(exc))
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


async def learn_style_source(ctx: object, profile_id: str) -> None:
    settings = get_settings()
    from sqlalchemy import select

    from app.infrastructure.persistence.models.style import UserStyleProfile

    async with AsyncSessionLocal() as session:
        try:
            profile = await session.scalar(
                select(UserStyleProfile).where(
                    UserStyleProfile.id == uuid.UUID(profile_id)
                )
            )
            if profile is None:
                logger.warning("style_learn_profile_not_found", profile_id=profile_id)
                return

            profile.learn_status = "processing"
            await session.flush()

            # attempt link extraction for source_url (best-effort, no hard failure)
            if profile.source_url:
                try:
                    from app.infrastructure.importers.router import route_extract_image_url
                    extracted = await route_extract_image_url(profile.source_url, settings)
                    if extracted and not profile.image_url:
                        profile.image_url = extracted
                except Exception as exc:
                    logger.warning(
                        "style_learn_link_extract_failed",
                        profile_id=profile_id,
                        error=str(exc),
                    )

            # Real visual learning (flag-gated) when we have an image; else the
            # legacy URL-domain heuristic. Only the vision path writes the
            # consumed style_tags (so it can drive the template matcher); the
            # heuristic path stays summary-only to keep flag-off a clean rollback.
            learned = await _learn_from_image(
                profile.image_url or "", profile.user_id, profile_id, settings
            )
            if learned is not None:
                style_tags = learned["tags"]
                profile.style_tags = {"tags": style_tags}
                profile.learned_summary = learned
            else:
                # Flag off, no image, or vision failed → today's behavior exactly:
                # learned_summary only, style_tags untouched (clean rollback).
                style_tags = _infer_style_tags(profile.source_url or profile.image_url)
                profile.learned_summary = {
                    "style_tags": style_tags,
                    "color_palette": [],
                    "avoid_tags": [],
                    "confidence": 0.5,
                    "source": "heuristic",
                }
            profile.learn_status = "learned"
            profile.learned_at = datetime.now(UTC)
            user_id = profile.user_id
            await session.commit()
            logger.info("style_learn_completed", profile_id=profile_id, style_tags=style_tags)

            # The vision path wrote recommendation-visible style_tags (consumed by
            # style_context in /today). The request-time cache invalidation ran
            # before this async work finished, so drop the now-stale /today cache
            # for this user. Best-effort: the helper swallows Redis errors.
            if learned is not None:
                from app.infrastructure.cache.client import (
                    get_redis_client,
                    invalidate_daily_recommendation_cache,
                )

                await invalidate_daily_recommendation_cache(get_redis_client(), user_id)

        except Exception as exc:
            await session.rollback()
            logger.warning("style_learn_failed", profile_id=profile_id, error=str(exc))
            try:
                async with AsyncSessionLocal() as err_session:
                    profile = await err_session.scalar(
                        select(UserStyleProfile).where(
                            UserStyleProfile.id == uuid.UUID(profile_id)
                        )
                    )
                    if profile is not None:
                        profile.learn_status = "failed"
                        await err_session.commit()
            except Exception:
                pass
