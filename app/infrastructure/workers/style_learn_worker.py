import uuid
from datetime import UTC, datetime

import structlog

from app.application.style.inspiration_analysis import apply_visual_style, learn_from_image
from app.config.settings import get_settings
from app.infrastructure.persistence.session import AsyncSessionLocal

logger = structlog.get_logger(__name__)

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
            learned = await learn_from_image(
                profile.image_url or "", profile.user_id, profile_id, settings
            )
            if learned is not None:
                style_tags = learned["tags"]
                apply_visual_style(profile, learned)
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
