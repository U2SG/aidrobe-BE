"""Backfill localized names for legacy wardrobe items (admin repair).

Items imported before naming localization (BE#30) carry an AI-generated —
usually English — `title` and no `localized_names`, so every surface renders
English to zh-CN users. One LLM call per batch turns those titles into
zh-CN/en-US pairs; user-renamed items (`name_source != 'ai'`) are never
touched, and per-item parse gaps are skipped rather than failing the batch.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

import structlog
from sqlalchemy import ColumnElement, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ports.llm import LlmPort
from app.infrastructure.persistence.models.wardrobe import WardrobeItem

logger = structlog.get_logger(__name__)

_SYSTEM_PROMPT = (
    "You translate garment names for a wardrobe app. For every entry in the "
    "user's JSON list, produce a natural Simplified Chinese name (zh) and a "
    "natural English name (en) for the garment — concise product-style names, "
    "no embellishment. Respond with ONLY a JSON object of the shape "
    '{"names": [{"idx": <same idx as input>, "zh": "...", "en": "..."}]} '
    "covering every input entry."
)


@dataclass(frozen=True)
class BackfillResult:
    scanned: int
    updated: int
    remaining: int


def _needs_backfill_clause() -> tuple[ColumnElement[bool], ...]:
    return (
        WardrobeItem.name_source == "ai",
        WardrobeItem.localized_names.is_(None),
        WardrobeItem.deleted_at.is_(None),
    )


async def backfill_missing_localized_names(
    session: AsyncSession, llm: LlmPort, *, limit: int
) -> BackfillResult:
    rows = list(
        await session.scalars(
            select(WardrobeItem).where(*_needs_backfill_clause()).order_by(WardrobeItem.id).limit(limit)
        )
    )
    if not rows:
        return BackfillResult(scanned=0, updated=0, remaining=0)

    payload = [
        {"idx": idx, "title": row.title, "category": row.category, "color": row.color_main}
        for idx, row in enumerate(rows)
    ]
    answer = await llm.ask(_SYSTEM_PROMPT, json.dumps(payload, ensure_ascii=False), json_mode=True)
    names_by_idx: dict[int, tuple[str, str]] = {}
    try:
        data = json.loads(answer.text)
        for entry in data.get("names", []):
            idx, zh, en = entry.get("idx"), entry.get("zh"), entry.get("en")
            if isinstance(idx, int) and isinstance(zh, str) and isinstance(en, str) and zh and en:
                names_by_idx[idx] = (zh.strip()[:120], en.strip()[:120])
    except ValueError:
        logger.warning("backfill_item_names.parse_failed")

    updated = 0
    for idx, row in enumerate(rows):
        pair = names_by_idx.get(idx)
        if pair is None:
            continue
        zh, en = pair
        row.localized_names = {"zh-CN": zh, "en-US": en}
        if not row.canonical_name:
            # The stable English canonical the naming provenance expects; the
            # legacy English title is exactly that.
            row.canonical_name = en
        updated += 1
    await session.flush()

    remaining = (
        await session.scalar(
            select(func.count()).select_from(WardrobeItem).where(*_needs_backfill_clause())
        )
    ) or 0
    logger.info(
        "backfill_item_names.batch",
        scanned=len(rows),
        updated=updated,
        remaining=remaining,
    )
    return BackfillResult(scanned=len(rows), updated=updated, remaining=remaining)
