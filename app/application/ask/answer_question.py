"""M15 Ask Aidrobe use case.

Assembles a LIGHTWEIGHT, sanitized view of the user's wardrobe, calls the
LLM through `LlmPort.ask`, persists the turn to `ask_sessions`, and records
the token cost on `async_jobs` so the cost guard (`job_type="llm_ask"`) can
enforce the per-user-per-day limit off the DB-aggregate ground truth.

Privacy: wardrobe rows go through `app.infrastructure.llm.sanitizer` — only
the §2.9 whitelist of structured attributes (category/color/status/location/
season_tags/fit_*) plus an opaque HMAC user token reach the prompt. NEVER
images, titles, or the raw user_id.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.cost_guard import assert_within_quota
from app.application.ports.llm import LlmPort
from app.common import error_codes
from app.common.enums import ItemStatus
from app.common.exceptions import FriendlyException
from app.infrastructure.llm.sanitizer import hash_user_token, sanitize_for_llm
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.ask_repo import SqlAlchemyAskRepository

# Cap the number of items injected so the prompt stays lightweight and cheap.
_MAX_ITEMS = 80
# Projected token cost used for the pre-call quota reservation when the
# provider hasn't answered yet (DB-aggregate path reads this as the delta).
_PROJECTED_TOKENS = 1_500
_JOB_TYPE = "llm_ask"

_SYSTEM_PREAMBLE = (
    "你是 Aidrobe 的穿搭助理。基于用户提供的衣橱结构化数据回答穿搭问题，"
    "建议要具体、可执行，只用中文回答。以下是用户的衣橱（已脱敏，仅含结构化属性）：\n"
)


@dataclass(frozen=True)
class AskResult:
    ask_session_id: uuid.UUID
    answer: str
    item_refs: list[str]
    tokens_used: int


def _item_to_sanitized(item: WardrobeItem) -> dict[str, Any]:
    """Project a wardrobe row to the sanitizer-whitelisted fields only."""
    raw = {
        "category": item.category,
        "color_main": item.color_main,
        "status": item.status,
        "location": item.location,
        "season_tags": item.season_tags,
        "fit_status": item.fit_status,
        "fit_intent": item.fit_intent,
    }
    return sanitize_for_llm(raw)


def _build_system_prompt(user_id: uuid.UUID, items: list[WardrobeItem]) -> str:
    # Items are already field-sanitized by `_item_to_sanitized`; build the
    # final payload directly and attach the opaque user token. Do NOT re-run
    # the container through `sanitize_for_llm` — its whitelist has no "items"
    # key, so a second pass would silently strip the entire wardrobe.
    payload = {
        "items": [_item_to_sanitized(i) for i in items],
        "usr": hash_user_token(user_id),
    }
    return _SYSTEM_PREAMBLE + json.dumps(payload, ensure_ascii=False)


async def answer_question(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    question: str,
    context: dict[str, Any] | None,
    llm: LlmPort,
) -> AskResult:
    # Quota first — refuses BEFORE any LLM spend. DB-aggregate path (no
    # redis_client) so tests are deterministic; no-op when ENABLE_COST_GUARD
    # is off.
    await assert_within_quota(
        session, user_id, _JOB_TYPE, cost_tokens=_PROJECTED_TOKENS
    )

    items = list(
        await session.scalars(
            select(WardrobeItem)
            .where(
                WardrobeItem.user_id == user_id,
                WardrobeItem.status == ItemStatus.WEARABLE.value,
                WardrobeItem.deleted_at.is_(None),
            )
            .limit(_MAX_ITEMS)
        )
    )

    system_prompt = _build_system_prompt(user_id, items)
    answer = await llm.ask(system_prompt, question)

    if not answer.text:
        # Provider not configured (LLM_PROVIDER=none) — degrade gracefully.
        raise FriendlyException(
            error_codes.INTERNAL_ERROR,
            "Ask is not available",
            status_code=503,
        )

    # v1 simplification: reference every item we fed the prompt rather than
    # parsing free-text citations (brittle, untestable with a fake LLM).
    item_refs = [str(i.id) for i in items]
    tokens_used = answer.tokens_used

    # Record cost on async_jobs — the cost-guard ground truth. Inline work,
    # so we write a terminal `succeeded` row directly rather than enqueue.
    ask_repo = SqlAlchemyAskRepository(session)
    ask_row = await ask_repo.create(
        user_id=user_id,
        question=question,
        answer=answer.text,
        item_refs=item_refs,
        context=context,
        tokens_used=tokens_used,
    )
    session.add(
        AsyncJob(
            job_type=_JOB_TYPE,
            idempotency_key=f"{_JOB_TYPE}:{ask_row.id}",
            user_id=user_id,
            target_id=ask_row.id,
            status="succeeded",
            attempts=1,
            cost_tokens=tokens_used,
        )
    )
    await session.flush()

    return AskResult(
        ask_session_id=ask_row.id,
        answer=answer.text,
        item_refs=item_refs,
        tokens_used=tokens_used,
    )


async def list_history(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    limit: int,
    offset: int,
) -> list[Any]:
    return await SqlAlchemyAskRepository(session).list_for_user(
        user_id, limit=limit, offset=offset
    )
