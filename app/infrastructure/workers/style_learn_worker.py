import uuid
from datetime import UTC, datetime

import structlog

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
    from app.infrastructure.persistence.models.style import UserStyleProfile
    from sqlalchemy import select

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

            style_tags = _infer_style_tags(profile.source_url or profile.image_url)

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

            profile.learned_summary = {
                "style_tags": style_tags,
                "color_palette": [],
                "avoid_tags": [],
                "confidence": 0.5,
            }
            profile.learn_status = "learned"
            profile.learned_at = datetime.now(UTC)
            await session.commit()
            logger.info("style_learn_completed", profile_id=profile_id, style_tags=style_tags)

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
