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
from datetime import UTC, datetime
from typing import Any

import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ask.wardrobe_context import (
    WardrobeContext,
    build_wardrobe_context,
    context_for_prompt,
    context_snapshot,
)
from app.application.cost_guard import assert_within_quota
from app.application.ports.llm import LlmPort
from app.common import error_codes
from app.common.enums import ItemStatus
from app.common.exceptions import FriendlyException
from app.config.settings import get_settings
from app.infrastructure.llm.sanitizer import hash_user_token, sanitize_for_llm
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.ask_repo import SqlAlchemyAskRepository

# Cap the number of items injected so the prompt stays lightweight and cheap.
_MAX_ITEMS = 80
# Pre-call quota reservation. The real cost is recorded post-call from the
# provider's token count; this is only the admission estimate, so it scales
# with the prompt size (preamble + question + N sanitized items) instead of a
# flat guess that under-counts a full wardrobe.
_BASE_PROJECTED_TOKENS = 400
_TOKENS_PER_ITEM = 25
_JOB_TYPE = "llm_ask"


def _project_tokens(item_count: int) -> int:
    return _BASE_PROJECTED_TOKENS + item_count * _TOKENS_PER_ITEM

logger = structlog.get_logger(__name__)

# Locale-keyed system preambles. Default (and any unknown locale) falls back
# to Chinese to preserve the pre-locale behavior. Match on the language
# subtag only ("en-US" -> "en") so client BCP-47 variants all resolve.
_PREAMBLES: dict[str, str] = {
    "zh": (
        "你是 Aidrobe 的穿搭助理。基于用户提供的衣橱结构化数据回答穿搭问题，"
        "建议要具体、可执行，只用中文回答。以下是用户的衣橱（已脱敏，仅含结构化属性）：\n"
    ),
    "en": (
        "You are Aidrobe's styling assistant. Answer styling questions using the "
        "user's structured wardrobe data. Be specific and actionable, and respond "
        "only in English. The wardrobe (sanitized, structured attributes only) "
        "follows:\n"
    ),
}
_DEFAULT_LANG = "zh"

# Appended when the caller scoped the Ask to one item; that item is placed
# first in the candidate list, so the clause refers to position #1.
_FOCUS_CLAUSE: dict[str, str] = {
    "zh": "用户希望围绕列表中第 1 件单品来搭配，请优先考虑它。\n",
    "en": "The user wants to build an outfit around the FIRST item in the list; prioritize it.\n",
}


# Appended when ENABLE_ASK_STRUCTURED_OUTPUT is on. Asks for a strict JSON
# envelope so the server can validate which items the model actually cited.
# `referenced_items` are 1-based positions into the items list above.
_STRUCTURED_INSTRUCTION: dict[str, str] = {
    "zh": (
        '\n请只返回一个 JSON 对象，不要包含任何其它文字：'
        '{"answer": "你的中文建议", "referenced_items": [引用到的单品在上面列表中的序号，从 1 开始]}。'
        "若没有引用任何单品，referenced_items 返回空数组。"
    ),
    "en": (
        "\nRespond with a single JSON object and nothing else: "
        '{"answer": "your advice in English", "referenced_items": [1-based positions '
        "of the items above you reference]}. If you reference no item, return an empty array."
    ),
}


def _resolve_lang(locale: str | None) -> str:
    lang = (locale or "").split("-", 1)[0].lower()
    return lang if lang in _PREAMBLES else _DEFAULT_LANG


def _preamble_for(locale: str | None) -> str:
    return _PREAMBLES[_resolve_lang(locale)]


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


def _build_system_prompt(
    user_id: uuid.UUID,
    items: list[WardrobeItem],
    ctx: WardrobeContext,
    *,
    structured: bool = False,
) -> str:
    # Items are already field-sanitized by `_item_to_sanitized`; the context
    # block is derived/bounded (see wardrobe_context safety story). Build the
    # final payload directly and attach the opaque user token. Do NOT re-run
    # the container through `sanitize_for_llm` — its whitelist has no "items"
    # key, so a second pass would silently strip the entire wardrobe.
    lang = ctx.locale  # already resolved to a language subtag
    preamble = _PREAMBLES.get(lang, _PREAMBLES[_DEFAULT_LANG])
    if ctx.scope == "item":
        preamble += _FOCUS_CLAUSE.get(lang, _FOCUS_CLAUSE[_DEFAULT_LANG])
    payload = {
        "context": context_for_prompt(ctx),
        "items": [_item_to_sanitized(i) for i in items],
        "usr": hash_user_token(user_id),
    }
    prompt = preamble + json.dumps(payload, ensure_ascii=False)
    if structured:
        prompt += _STRUCTURED_INSTRUCTION.get(lang, _STRUCTURED_INSTRUCTION[_DEFAULT_LANG])
    return prompt


def _parse_structured(text: str, item_count: int) -> tuple[str, list[int]] | None:
    """Parse the structured-output envelope.

    Returns (answer_text, validated 0-based item indices) or None when the
    response isn't the expected JSON shape. Positions are 1-based in the
    prompt; only those that land inside [1, item_count] survive — anything
    else is a hallucinated reference and is dropped."""
    try:
        data = json.loads(text)
    except ValueError:  # JSONDecodeError is a ValueError subclass
        return None
    if not isinstance(data, dict):
        return None
    answer = data.get("answer")
    refs = data.get("referenced_items", [])
    if not isinstance(answer, str) or not isinstance(refs, list):
        return None
    seen: set[int] = set()
    indices: list[int] = []
    for pos in refs:
        if isinstance(pos, bool) or not isinstance(pos, int):
            continue
        if 1 <= pos <= item_count and pos not in seen:
            seen.add(pos)
            indices.append(pos - 1)
    return answer, indices


async def answer_question(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    question: str,
    context: dict[str, Any] | None,
    llm: LlmPort,
    locale: str | None = None,
    focused_item_id: uuid.UUID | None = None,
) -> AskResult:
    # Validate the focused item before any quota/LLM work. Per repo convention
    # a non-owned (or soft-deleted) resource returns 404 — never reveal that
    # someone else's item exists.
    focused_item: WardrobeItem | None = None
    if focused_item_id is not None:
        focused_item = await session.get(WardrobeItem, focused_item_id)
        if (
            focused_item is None
            or focused_item.user_id != user_id
            or focused_item.deleted_at is not None
        ):
            raise FriendlyException(
                error_codes.NOT_FOUND,
                "Item not found",
                status_code=404,
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

    if focused_item is not None:
        # Guarantee the focused item is present and first, even if it's not
        # wearable or fell outside the wearable-items limit. Dedupe so it
        # isn't listed twice.
        items = [focused_item] + [i for i in items if i.id != focused_item.id]
        items = items[:_MAX_ITEMS]

    # Quota — refuses BEFORE any LLM spend (the item fetch above is a cheap DB
    # read, not spend). Projecting from the assembled candidate count keeps the
    # reservation honest as the prompt grows. DB-aggregate path (no
    # redis_client) so tests are deterministic; no-op when ENABLE_COST_GUARD off.
    await assert_within_quota(
        session, user_id, _JOB_TYPE, cost_tokens=_project_tokens(len(items))
    )

    structured = get_settings().ENABLE_ASK_STRUCTURED_OUTPUT
    ctx = await build_wardrobe_context(
        session,
        user_id,
        items=items,
        focused_item_id=focused_item_id,
        locale=_resolve_lang(locale),
        today=datetime.now(UTC).date(),
    )
    system_prompt = _build_system_prompt(user_id, items, ctx, structured=structured)
    answer = await llm.ask(system_prompt, question, json_mode=structured)

    if not answer.text:
        # Provider not configured (LLM_PROVIDER=none) — degrade gracefully.
        raise FriendlyException(
            error_codes.INTERNAL_ERROR,
            "Ask is not available",
            status_code=503,
        )

    answer_text = answer.text
    item_refs: list[str]
    if structured:
        parsed = _parse_structured(answer.text, len(items))
        if parsed is None:
            # The model ignored the JSON contract (distinct from a provider
            # error, which surfaces above as empty text). Stay honest: keep the
            # raw text as the answer but emit no item_refs rather than risk
            # surfacing items the model never cited.
            logger.warning("ask.structured_output.parse_failed")
            item_refs = []
        else:
            answer_text, indices = parsed
            item_refs = [str(items[i].id) for i in indices]
    else:
        # Legacy behavior (flag off): reference every item fed to the prompt.
        item_refs = [str(i.id) for i in items]
    tokens_used = answer.tokens_used

    # Persist the context snapshot alongside the client-supplied context
    # (date/scene/time_left) so the answer stays traceable to the wardrobe
    # state it was grounded in.
    persisted_context = {**(context or {}), "snapshot": context_snapshot(ctx)}

    # Record cost on async_jobs — the cost-guard ground truth. Inline work,
    # so we write a terminal `succeeded` row directly rather than enqueue.
    ask_repo = SqlAlchemyAskRepository(session)
    ask_row = await ask_repo.create(
        user_id=user_id,
        question=question,
        answer=answer_text,
        item_refs=item_refs,
        context=persisted_context,
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
        answer=answer_text,
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
