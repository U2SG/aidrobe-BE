"""Style context builder for M13 — style-aware recommendation display."""

import uuid
from collections import Counter
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.persistence.models.style import UserStyleProfile

# Template mapping: dominant tag sets → template_id
_TEMPLATE_RULES: list[tuple[set[str], str]] = [
    ({"black", "white", "gray", "grey", "minimal", "clean"}, "minimal_commute_v1"),
    ({"pink", "soft", "feminine", "floral", "pastel"}, "soft_feminine_v1"),
    ({"formal", "suit", "professional", "office"}, "formal_office_v1"),
    ({"casual", "street", "sporty", "denim"}, "casual_street_v1"),
]
_DEFAULT_TEMPLATE = "general_v1"


async def build_style_context(
    user_id: uuid.UUID, session: AsyncSession
) -> dict[str, Any]:
    result = await session.execute(
        select(UserStyleProfile).where(
            UserStyleProfile.user_id == user_id,
            UserStyleProfile.learn_status == "learned",
        )
    )
    profiles = list(result.scalars().all())

    tag_counter: Counter[str] = Counter()
    for p in profiles:
        tags: Any = p.style_tags or []
        if isinstance(tags, list):
            tag_counter.update(tags)

    if not tag_counter:
        return {
            "template_id": _DEFAULT_TEMPLATE,
            "active_tags": [],
            "confidence": 0.0,
        }

    top_tags = [t for t, _ in tag_counter.most_common(10)]
    tag_set = set(top_tags)
    total_signals = sum(tag_counter.values())

    template_id = _DEFAULT_TEMPLATE
    best_overlap = 0
    for rule_tags, tmpl in _TEMPLATE_RULES:
        overlap = len(tag_set & rule_tags)
        if overlap > best_overlap:
            best_overlap = overlap
            template_id = tmpl

    # tag hit rate (0–1): how many top tags matched the winning template
    if best_overlap > 0:
        winning_tags = _TEMPLATE_RULES[
            [t for _, t in _TEMPLATE_RULES].index(template_id)
        ][0]
        hit_rate = best_overlap / len(winning_tags)
    else:
        hit_rate = 0.0
    # data confidence (0–1): saturates at 10 learned signals
    data_confidence = min(1.0, total_signals / 10)
    confidence = round(hit_rate * data_confidence, 3)

    return {
        "template_id": template_id,
        "active_tags": top_tags[:5],
        "confidence": confidence,
    }
