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
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any

import structlog
from sqlalchemy import nulls_last, select
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
from app.common.locale import lang_subtag, resolve_locale
from app.config.settings import get_settings
from app.infrastructure.llm.sanitizer import hash_user_token, sanitize_for_llm
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.user import UserSettings
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.ask_repo import SqlAlchemyAskRepository

# Cap the number of items injected so the prompt stays lightweight and cheap.
_MAX_ITEMS = 80
# BE#27 — pull a priority-ordered superset, then curate down to `_MAX_ITEMS`.
# Fetching more than the cap lets the curation step balance category coverage
# (e.g. surface rarely-worn shoes) instead of letting a SQL `LIMIT` truncate by
# the default row order. 200 covers essentially every real wardrobe.
_CANDIDATE_FETCH_CAP = 200
# Same (category, color_main) pieces beyond this are sampled out so a closet
# full of white shirts doesn't crowd the prompt and starve other categories.
_MAX_PER_COLOR_CATEGORY = 3
# Round-robin order for category coverage; key categories first, others appended
# alphabetically after these (see `_curate_candidates`).
_CATEGORY_PRIORITY = ("top", "bottom", "outerwear", "dress", "shoes")
# Bump when the per-item field set (`_item_to_sanitized`) changes, so persisted
# snapshots are attributable to the prompt schema that produced them.
_ITEM_FIELD_SCHEMA_VERSION = 2
# `style_tags` is user-typed; bound count + length before it reaches a prompt.
_MAX_STYLE_TAGS_PER_ITEM = 8
_MAX_STYLE_TAG_LEN = 50
# Pre-call quota reservation. The real cost is recorded post-call from the
# provider's token count; this is only the admission estimate, so it scales
# with the prompt size (preamble + question + N sanitized items) instead of a
# flat guess that under-counts a full wardrobe. Bumped from 25 with BE#27's
# richer per-item field set.
_BASE_PROJECTED_TOKENS = 400
_TOKENS_PER_ITEM = 45
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
        "\n请只返回一个 JSON 对象，不要包含任何其它文字："
        '{"answer": "你的中文建议", "referenced_items": [引用到的单品在上面列表中的序号，从 1 开始]}。'
        "若没有引用任何单品，referenced_items 返回空数组。"
    ),
    "en": (
        "\nRespond with a single JSON object and nothing else: "
        '{"answer": "your advice in English", "referenced_items": [1-based positions '
        "of the items above you reference]}. If you reference no item, return an empty array."
    ),
}


def _resolve_lang(locale: str | None, settings_locale: str | None = None) -> str:
    # BE#30 — request locale wins, else the user's persisted settings locale,
    # else the zh-CN product default. resolve_locale always returns a supported
    # BCP-47 locale, so its subtag is guaranteed present in _PREAMBLES.
    return lang_subtag(resolve_locale(locale, settings_locale))


def _preamble_for(locale: str | None) -> str:
    return _PREAMBLES[_resolve_lang(locale)]


@dataclass(frozen=True)
class AskResult:
    ask_session_id: uuid.UUID
    answer: str
    item_refs: list[str]
    tokens_used: int
    # Effective BCP-47 locale (request > settings > default) already resolved
    # here, so the router can build snapshots without re-querying settings.
    effective_locale: str


@dataclass(frozen=True)
class ItemRefSnapshot:
    """BE#29 — denormalized card for one referenced wardrobe item.

    `name` is already locale-resolved (localized display name, title fallback);
    category/color_main stay stable English. `thumbnail_object_key` /
    `thumbnail_fallback_url` are handed to the presentation layer so it can
    sign a short-lived URL at request time rather than persisting one."""

    item_id: uuid.UUID
    name: str
    category: str
    color_main: str
    thumbnail_object_key: str | None
    thumbnail_fallback_url: str | None


def _resolve_item_name(item: WardrobeItem, locale: str) -> str:
    # BE#30 — prefer the localized display name for the resolved locale, else
    # the user-visible title. `localized_names` keys are full BCP-47 locales
    # (e.g. "zh-CN"); resolve_locale yields exactly those.
    names = item.localized_names
    if isinstance(names, dict):
        localized = names.get(locale)
        if isinstance(localized, str) and localized:
            return localized
    return item.title


async def build_item_ref_snapshots(
    session: AsyncSession,
    user_id: uuid.UUID,
    item_ids: list[str],
    *,
    locale: str | None = None,
    settings_locale: str | None = None,
) -> list[ItemRefSnapshot]:
    """Batch-fetch snapshots for a set of item_refs (one query for the set).

    Only the caller's accessible, non-deleted items survive; deleted / foreign
    ids fall out. Order follows the requested `item_ids` so the cards line up
    with item_refs. The thumbnail is left unsigned here — the presentation
    layer signs `thumbnail_object_key` on demand."""
    parsed: list[uuid.UUID] = []
    seen: set[uuid.UUID] = set()
    for ref in item_ids:
        try:
            parsed_id = uuid.UUID(ref)
        except (ValueError, AttributeError):
            continue
        if parsed_id not in seen:
            seen.add(parsed_id)
            parsed.append(parsed_id)
    if not parsed:
        return []
    resolved_locale = resolve_locale(locale, settings_locale)
    rows = await SqlAlchemyAskRepository(session).fetch_item_ref_snapshots(user_id, parsed)
    by_id = {item.id: (item, object_key, fallback) for item, object_key, fallback in rows}
    snapshots: list[ItemRefSnapshot] = []
    for item_id in parsed:
        found = by_id.get(item_id)
        if found is None:
            continue
        item, object_key, fallback = found
        snapshots.append(
            ItemRefSnapshot(
                item_id=item.id,
                name=_resolve_item_name(item, resolved_locale),
                category=item.category,
                color_main=item.color_main,
                thumbnail_object_key=object_key,
                thumbnail_fallback_url=fallback,
            )
        )
    return snapshots


def _bound_style_tags(tags: Any) -> list[str] | None:
    """Cap user-typed `style_tags` (count + per-tag length) before it reaches a
    prompt. Returns None when empty so `_item_to_sanitized` can drop the key."""
    if not isinstance(tags, list):
        return None
    bounded = [t[:_MAX_STYLE_TAG_LEN] for t in tags if isinstance(t, str) and t][
        :_MAX_STYLE_TAGS_PER_ITEM
    ]
    return bounded or None


def _item_to_sanitized(item: WardrobeItem, today: date) -> dict[str, Any]:
    """Project a wardrobe row to the sanitizer-whitelisted fields only.

    BE#27 — beyond core category/color/status, emit the extended styling signals
    (sub_category/material/formality/time_cost/wear_count/...) so the model can
    reason about coordination. Recency is a derived `last_worn_days_ago` bucket,
    never the raw date. `style_tags` is user-typed free text, so it's bounded
    here before the sanitizer key-gate. Null fields are dropped to keep the
    prompt compact now that the field set is wider."""
    raw: dict[str, Any] = {
        "category": item.category,
        "sub_category": item.sub_category,
        "color_main": item.color_main,
        "color_secondary": item.color_secondary,
        "material": item.material,
        "status": item.status,
        "location": item.location,
        "season_tags": item.season_tags,
        "style_tags": _bound_style_tags(item.style_tags),
        "formality_score": item.formality_score,
        "time_cost_level": item.time_cost_level,
        "fit_status": item.fit_status,
        "fit_intent": item.fit_intent,
        "wear_count": item.wear_count,
        "cutout_status": item.cutout_status,
    }
    if item.last_worn_at is not None:
        raw["last_worn_days_ago"] = max((today - item.last_worn_at).days, 0)
    return sanitize_for_llm({k: v for k, v in raw.items() if v is not None})


def _curate_candidates(
    pool: list[WardrobeItem],
    focused_item: WardrobeItem | None,
    *,
    max_items: int,
) -> list[WardrobeItem]:
    """Pick a stable, explainable candidate set from the priority-ordered `pool`
    (already sorted by wear_count/recency in SQL).

    1. Drop near-duplicates: cap each (category, color_main) bucket to
       `_MAX_PER_COLOR_CATEGORY` so similar pieces don't crowd out variety.
    2. Round-robin across categories (key categories first) so the prompt covers
       top/bottom/outerwear/shoes rather than N near-identical shirts.
    3. The focused item, if any, is pinned to position 1 and never deduped away.
    """
    focused_id = focused_item.id if focused_item is not None else None
    seen_color_cat: Counter[tuple[str, str | None]] = Counter()
    buckets: dict[str, list[WardrobeItem]] = {}
    for item in pool:
        if focused_id is not None and item.id == focused_id:
            continue
        key = (item.category, item.color_main)
        if seen_color_cat[key] >= _MAX_PER_COLOR_CATEGORY:
            continue
        seen_color_cat[key] += 1
        buckets.setdefault(item.category, []).append(item)

    ordered_cats = sorted(
        buckets,
        key=lambda c: (
            _CATEGORY_PRIORITY.index(c) if c in _CATEGORY_PRIORITY else len(_CATEGORY_PRIORITY),
            c,
        ),
    )
    budget = max_items - (1 if focused_item is not None else 0)
    selected: list[WardrobeItem] = []
    cursors = dict.fromkeys(ordered_cats, 0)
    while len(selected) < budget:
        progressed = False
        for cat in ordered_cats:
            if len(selected) >= budget:
                break
            i = cursors[cat]
            bucket = buckets[cat]
            if i < len(bucket):
                selected.append(bucket[i])
                cursors[cat] = i + 1
                progressed = True
        if not progressed:
            break

    return ([focused_item] if focused_item is not None else []) + selected


def _candidate_selection_meta(
    items: list[WardrobeItem], focused_item: WardrobeItem | None
) -> dict[str, Any]:
    """Snapshot-only metadata describing how the candidate set was chosen, for
    explainability / answer-quality tracking. Never reaches the prompt."""
    by_category = Counter(i.category for i in items if i.category)
    return {
        "strategy": "wear_recency_priority+color_cat_dedupe+category_roundrobin",
        "count_by_category": dict(by_category),
        "focused_in_top1": bool(
            focused_item is not None and items and items[0].id == focused_item.id
        ),
        "prompt_field_schema_version": _ITEM_FIELD_SCHEMA_VERSION,
    }


def _build_system_prompt(
    user_id: uuid.UUID,
    items: list[WardrobeItem],
    ctx: WardrobeContext,
    today: date,
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
        "items": [_item_to_sanitized(i, today) for i in items],
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
        if focused_item is None or focused_item.user_id != user_id or focused_item.deleted_at is not None:
            raise FriendlyException(
                error_codes.NOT_FOUND,
                "Item not found",
                status_code=404,
            )

    # Pull a priority-ordered superset (most-worn / most-recent first) so the
    # curation step can balance category coverage instead of letting a bare
    # `LIMIT` truncate by default row order. NULLs-last keeps never-worn items
    # from sorting to the top under DESC.
    pool = list(
        await session.scalars(
            select(WardrobeItem)
            .where(
                WardrobeItem.user_id == user_id,
                WardrobeItem.status == ItemStatus.WEARABLE.value,
                WardrobeItem.deleted_at.is_(None),
            )
            .order_by(
                WardrobeItem.wear_count.desc(),
                nulls_last(WardrobeItem.last_worn_at.desc()),
                WardrobeItem.updated_at.desc(),
                WardrobeItem.id,
            )
            .limit(_CANDIDATE_FETCH_CAP)
        )
    )
    # Curate to a stable, explainable set; the focused item (if any) is pinned
    # first even when it's not wearable or fell outside the fetch cap.
    items = _curate_candidates(pool, focused_item, max_items=_MAX_ITEMS)
    today = datetime.now(UTC).date()

    # Quota — refuses BEFORE any LLM spend (the item fetch above is a cheap DB
    # read, not spend). Projecting from the assembled candidate count keeps the
    # reservation honest as the prompt grows. DB-aggregate path (no
    # redis_client) so tests are deterministic; no-op when ENABLE_COST_GUARD off.
    await assert_within_quota(session, user_id, _JOB_TYPE, cost_tokens=_project_tokens(len(items)))

    structured = get_settings().ENABLE_ASK_STRUCTURED_OUTPUT
    # BE#30 — when the request omits a locale, fall back to the user's
    # persisted settings locale (then the zh-CN default) so the answer
    # language tracks the user's UI choice.
    settings_locale = await session.scalar(select(UserSettings.locale).where(UserSettings.user_id == user_id))
    effective_locale = resolve_locale(locale, settings_locale)
    ctx = await build_wardrobe_context(
        session,
        user_id,
        items=items,
        focused_item_id=focused_item_id,
        locale=_resolve_lang(locale, settings_locale),
        today=today,
        candidate_selection=_candidate_selection_meta(items, focused_item),
    )
    system_prompt = _build_system_prompt(user_id, items, ctx, today, structured=structured)
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
    # BE#28 — item_refs must mean "items the model actually cited" (structured
    # success) or, failing that, only the explicitly focused item. We never
    # echo back the full candidate set: it implies references the model never
    # made and pollutes downstream snapshotting/analytics. The fallback (no
    # focus -> empty) is shared by the parse-failure and flag-off branches.
    fallback_refs = [str(focused_item.id)] if focused_item is not None else []
    if structured:
        parsed = _parse_structured(answer.text, len(items))
        if parsed is None:
            # The model ignored the JSON contract (distinct from a provider
            # error, which surfaces above as empty text). Stay honest: keep the
            # raw text as the answer but fall back to the focused item only.
            logger.warning("ask.structured_output.parse_failed")
            item_refs = fallback_refs
        else:
            answer_text, indices = parsed
            item_refs = [str(items[i].id) for i in indices]
    else:
        # Structured output disabled: we can't tell which items the model
        # cited, so item_refs degrades to the focused item only (or empty).
        logger.warning("ask.structured_output.disabled", item_refs_quality="limited")
        item_refs = fallback_refs
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
        effective_locale=effective_locale,
    )


async def list_history(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    limit: int,
    offset: int,
    focused_item_id: uuid.UUID | None = None,
) -> list[Any]:
    return await SqlAlchemyAskRepository(session).list_for_user(
        user_id, limit=limit, offset=offset, focused_item_id=focused_item_id
    )
