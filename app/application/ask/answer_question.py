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
import random
import re
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, date, datetime
from typing import Any, cast

import structlog
from sqlalchemy import func, nulls_last, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.ask.wardrobe_context import (
    WardrobeContext,
    build_wardrobe_context,
    context_for_prompt,
    context_snapshot,
)
from app.application.cost_guard import assert_within_quota
from app.application.llm_prompts.assemble import assemble_system_prompt
from app.application.llm_prompts.persona import stylist_persona
from app.application.llm_prompts.preference_memory import render_preferences
from app.application.llm_prompts.reasoning_policy import reasoning_policy
from app.application.llm_prompts.response_contracts import ask_structured_contract
from app.application.llm_prompts.self_critique import negative_preference_violations
from app.application.ports.llm import LlmPort
from app.application.preferences.memory import (
    SOURCE_EXPLICIT,
    PreferenceMemoryError,
    read_preference_memory,
    record_preference,
)
from app.application.safety.copy_guard import find_forbidden_terms, sanitize_generated_copy
from app.common import error_codes
from app.common.enums import ItemStatus
from app.common.exceptions import FriendlyException
from app.common.item_labels import localized_item_name
from app.common.locale import lang_subtag, resolve_locale
from app.config.settings import get_settings
from app.infrastructure.llm.sanitizer import hash_user_token, sanitize_for_llm, sanitize_llm_text
from app.infrastructure.persistence.models.ask_conversation import (
    AskConversationState,
    AskMessage,
)
from app.infrastructure.persistence.models.async_job import AsyncJob
from app.infrastructure.persistence.models.user import UserSettings
from app.infrastructure.persistence.models.wardrobe import WardrobeItem
from app.infrastructure.persistence.repositories.ask_conversation_repo import (
    SqlAlchemyAskConversationRepository,
)
from app.infrastructure.persistence.repositories.ask_repo import SqlAlchemyAskRepository
from app.infrastructure.persistence.repositories.preference_memory_repo import (
    SqlAlchemyPreferenceMemoryRepository,
)

# Cap the number of items injected so the prompt stays lightweight and cheap.
_MAX_ITEMS = 80
# BE#27 — pull a priority-ordered superset, then curate down to `_MAX_ITEMS`.
# Fetch the top-N PER CATEGORY (window query) rather than a flat global LIMIT:
# a flat LIMIT could drop a whole category (e.g. shoes) for a huge wardrobe
# whose top rows are all one category, defeating curation's coverage. A
# per-category cap guarantees every category reaches the curator while staying
# bounded (≈ categories × cap).
_PER_CATEGORY_FETCH_CAP = 40
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
_CONVERSATION_SUMMARY_THRESHOLD = 8
_RECENT_MESSAGES_TO_KEEP = 6
# Bound the summarizer reload: older messages are already folded into the
# carried-forward `summary`, so a fixed window of the most recent messages is
# enough. Keeps the path from reloading an ever-growing conversation.
_MAX_MESSAGES_TO_SUMMARIZE = 40
_MAX_CONVERSATION_SUMMARY_LEN = 600
_MAX_CONSTRAINTS = 8
_MAX_CONSTRAINT_LEN = 120
_REFINEMENT_MARKERS = (
    "更",
    "再",
    "换",
    "more",
    "less",
    "casual",
    "formal",
    "warmer",
    "cooler",
    "brighter",
    "darker",
)
# BE#87 — honest fallback when the LLM answer triggers the copy guard.
# Not a redirect: it tells the user we can't give a more detailed answer
# right now, without exposing what was filtered.
_ASK_FALLBACK_COPY = "这身搭配上我给不出更细的说法，但可以从舒服和得体的角度先选。"

# The contract forbids list positions in the user-facing answer (they are an
# internal referenced_items mechanism); these backstop the occasional
# violation across every forbidden format the prompt names: parenthesized
# ("白色上衣（序号1）", "the white shirt (item 2)") and bare ("序号1",
# "第 3 件", "item 2", "#3") — PR #109 review asked for full coverage.
_PAREN_ORDINAL_RE = re.compile(r"[（(]\s*(?:序号|编号|item|no\.?|#)\s*\d+\s*[)）]", re.IGNORECASE)
_BARE_ORDINAL_RE = re.compile(
    r"(?:序号|编号)\s*\d+|第\s*\d+\s*件(?:单品)?|\bitem\s+\d+\b|#\d+",
    re.IGNORECASE,
)


def _strip_ordinal_leaks(text: str) -> str:
    """Remove leaked list-position references from a user-facing answer."""
    cleaned = _PAREN_ORDINAL_RE.sub("", text)
    cleaned = _BARE_ORDINAL_RE.sub("", cleaned)
    if cleaned != text:
        logger.warning("ask.answer.ordinal_leak_stripped")
    return re.sub(r" {2,}", " ", cleaned).strip()


def _project_tokens(item_count: int) -> int:
    return _BASE_PROJECTED_TOKENS + item_count * _TOKENS_PER_ITEM


logger = structlog.get_logger(__name__)

# BE#66 — the persona voice now lives in the shared `llm_prompts.persona` module
# (one consistent stylist across Ask + Today). This Ask-specific directive only
# pins the RESPONSE LANGUAGE; everything else composes from the shared modules.
# Default (and any unknown locale) falls back to Chinese to preserve pre-locale
# behavior; match on the language subtag only ("en-US" -> "en").
_LANG_DIRECTIVE: dict[str, str] = {
    "zh": "请使用自然、地道的中文回答，建议要具体、可执行。",
    "en": "Respond only in natural English; keep advice specific and actionable.",
}
_DEFAULT_LANG = "zh"

_WARDROBE_CONTEXT_CLAUSE: dict[str, str] = {
    "zh": (
        "以下是用户的衣橱（已脱敏，仅含结构化属性）。只要 items 非空，"
        "就表示你已经拿到了可用的衣橱上下文；不要说“无法查看衣橱”、"
        "“无法直接查看您的衣橱内容”或类似表达。\n"
    ),
    "en": (
        "The wardrobe context follows (sanitized, structured attributes only). "
        "When items is non-empty, you do have usable wardrobe context; do not say "
        "\"I can't view your wardrobe\" or similar.\n"
    ),
}

_NO_WARDROBE_CONTEXT_CLAUSE: dict[str, str] = {
    "zh": (
        "本次没有可用的衣橱单品上下文。请只基于用户当前问题或当前单品信息回答，"
        "不要编造衣橱内容或引用任何单品。\n"
    ),
    "en": (
        "No wardrobe item context is available for this turn. Answer only from the "
        "current question or current item information, and do not invent wardrobe "
        "contents or reference any items.\n"
    ),
}

# Appended when the caller scoped the Ask to one item; that item is placed
# first in the candidate list, so the clause refers to position #1. Phrased as
# a hard requirement — "请优先考虑" was weak enough that the model kept giving
# its usual generic combo regardless of the focused piece (user report
# 2026-07-24).
_FOCUS_CLAUSE: dict[str, str] = {
    "zh": (
        "用户想围绕列表中第 1 件单品来搭配：整套答案必须以它为核心构建，"
        "referenced_items 必须包含 1，且要说明其它单品为什么衬它；"
        "不要给出与这件单品无关的通用搭配。\n"
    ),
    "en": (
        "The user wants an outfit built AROUND the FIRST item in the list: the answer "
        "must center on it, referenced_items must include 1, and the other pieces must "
        "be justified by how they complement it. Never give a generic combination that "
        "ignores this item.\n"
    ),
}


# Confidence for a preference the user stated explicitly in their own words.
# High, but not 1.0 — they may revise it later, and the row is user-correctable.
_EXPLICIT_PREFERENCE_CONFIDENCE = 0.9

def _resolve_lang(locale: str | None, settings_locale: str | None = None) -> str:
    # BE#30 — request locale wins, else the user's persisted settings locale,
    # else the zh-CN product default. resolve_locale always returns a supported
    # BCP-47 locale, so its subtag is guaranteed in the persona/clause maps.
    return lang_subtag(resolve_locale(locale, settings_locale))


def _preamble_for(locale: str | None) -> str:
    # BE#66 — the preamble is now the shared stylist persona.
    return stylist_persona(_resolve_lang(locale))


@dataclass(frozen=True)
class ConversationDeltas:
    reasoning: str | None
    applied_constraints: list[str]
    # BE#47 — structured "user rejected the focused item this turn" signal. The
    # trusted source for durable rejection (never substring-sniffed free text).
    rejected_focused_item: bool = False


@dataclass(frozen=True)
class TripIntent:
    """BE#115 — structured "this question is about a multi-day trip" signal, so
    the FE can hand off to the packing-list flow instead of leaving the user
    with a single-outfit answer."""

    is_trip: bool
    duration_days: int | None = None


@dataclass(frozen=True)
class AskResult:
    ask_session_id: uuid.UUID
    conversation_id: uuid.UUID
    answer: str
    item_refs: list[str]
    has_wardrobe_context: bool
    tokens_used: int
    deltas: ConversationDeltas
    # Effective BCP-47 locale (request > settings > default) already resolved
    # here, so the router can build snapshots without re-querying settings.
    effective_locale: str
    # Only the structured channel can surface trip intent; None elsewhere.
    trip_intent: TripIntent | None = None


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
    return localized_item_name(
        title=item.title,
        color_main=item.color_main,
        category=item.category,
        locale=locale,
        localized_names=item.localized_names if isinstance(item.localized_names, dict) else None,
        name_source=item.name_source,
    )


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
    # Shuffle within each category bucket so repeated asks don't present the
    # byte-identical, wear-count-ordered list every time — LLMs favor early
    # items, which made every answer converge on the same few pieces (user
    # report 2026-07-24). Category coverage and the dedupe cap are unaffected.
    for bucket in buckets.values():
        random.shuffle(bucket)
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
        "strategy": "wear_recency_priority+color_cat_dedupe+bucket_shuffle+category_roundrobin",
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
    # BE#66 — compose the system prompt from the shared `llm_prompts` modules
    # (persona / preference rules / reasoning policy / response contract) instead
    # of one monolith. Persona stays FIRST so the response-language behavior and
    # locale tests hold. Items are already field-sanitized by `_item_to_sanitized`;
    # the context block is derived/bounded (see wardrobe_context safety story).
    # The payload is attached directly with the opaque user token — do NOT re-run
    # the container through `sanitize_for_llm` (its whitelist has no "items" key,
    # so a second pass would silently strip the entire wardrobe).
    lang = ctx.locale  # already resolved to a language subtag
    if items:
        wardrobe_clause = _WARDROBE_CONTEXT_CLAUSE.get(lang, _WARDROBE_CONTEXT_CLAUSE[_DEFAULT_LANG])
    else:
        wardrobe_clause = _NO_WARDROBE_CONTEXT_CLAUSE.get(lang, _NO_WARDROBE_CONTEXT_CLAUSE[_DEFAULT_LANG])
    focus_clause = _FOCUS_CLAUSE.get(lang, _FOCUS_CLAUSE[_DEFAULT_LANG]) if ctx.scope == "item" else None
    payload = {
        "context": context_for_prompt(ctx),
        "items": [_item_to_sanitized(i, today) for i in items],
        "usr": hash_user_token(user_id),
    }
    return assemble_system_prompt(
        [
            stylist_persona(lang),
            _LANG_DIRECTIVE.get(lang, _LANG_DIRECTIVE[_DEFAULT_LANG]),
            wardrobe_clause,
            focus_clause,
            # Durable preferences as PRIORITIZED avoid/prefer rules (fixes
            # "included but not prioritized"); None when there are none.
            render_preferences(ctx.preference_memory, lang),
            json.dumps(payload, ensure_ascii=False),
            reasoning_policy(lang),
            ask_structured_contract(lang) if structured else None,
        ]
    )


def _extract_structured(
    data: dict[str, Any], item_count: int
) -> tuple[str, list[int], list[dict[str, str]]] | None:
    """Extract the structured-output envelope from an already-parsed dict.

    Returns (answer_text, validated 0-based item indices, stated_preferences) or
    None when the response isn't the expected JSON shape. Positions are 1-based
    in the prompt; only those that land inside [1, item_count] survive — anything
    else is a hallucinated reference and is dropped. `stated_preferences` is a
    best-effort list of {type, value, polarity} string dicts; the write path
    re-validates each, so anything malformed is simply not persisted."""
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
    preferences: list[dict[str, str]] = []
    raw_prefs = data.get("stated_preferences", [])
    if isinstance(raw_prefs, list):
        for pref in raw_prefs:
            if not isinstance(pref, dict):
                continue
            ptype, pvalue, ppolarity = pref.get("type"), pref.get("value"), pref.get("polarity")
            if isinstance(ptype, str) and isinstance(pvalue, str) and isinstance(ppolarity, str):
                preferences.append({"type": ptype, "value": pvalue, "polarity": ppolarity})
    return answer, indices, preferences


def _extract_trip_intent(data: dict[str, Any]) -> TripIntent | None:
    """BE#115 — pull the trip-intent block from the structured envelope.

    Returns None unless the model affirmatively set is_trip=true (missing /
    malformed blocks and is_trip=false all collapse to None so the response
    field stays absent for non-trip questions). duration_days survives only as
    a plausible integer (1–90); anything else degrades to None, not an error."""
    raw = data.get("trip_intent")
    if not isinstance(raw, dict) or raw.get("is_trip") is not True:
        return None
    duration = raw.get("duration_days")
    if isinstance(duration, bool) or not isinstance(duration, int) or not 1 <= duration <= 90:
        duration = None
    return TripIntent(is_trip=True, duration_days=duration)


def _extract_conversation_deltas(data: dict[str, Any]) -> ConversationDeltas:
    raw = data.get("conversation_deltas")
    if not isinstance(raw, dict):
        return ConversationDeltas(reasoning=None, applied_constraints=[])
    reasoning = raw.get("reasoning")
    constraints = raw.get("applied_constraints", [])
    # Copy guard (BE#87): deltas are LLM-generated and surface to the user via
    # AskResponse.deltas — reasoning with a forbidden term is dropped (None is
    # honest), offending constraints are filtered out.
    clean_reasoning = (
        sanitize_llm_text(reasoning, max_len=240) if isinstance(reasoning, str) else None
    )
    if clean_reasoning is not None and find_forbidden_terms(clean_reasoning):
        logger.warning("copy_guard.delta_reasoning_dropped")
        clean_reasoning = None
    clean_constraints = [
        c for c in _sanitize_constraint_list(constraints) if not find_forbidden_terms(c)
    ]
    return ConversationDeltas(
        reasoning=clean_reasoning,
        applied_constraints=clean_constraints,
        rejected_focused_item=raw.get("rejected_focused_item") is True,
    )


def _sanitize_constraint_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for value in values:
        if not isinstance(value, str):
            continue
        cleaned = sanitize_llm_text(value, max_len=_MAX_CONSTRAINT_LEN)
        if cleaned and cleaned not in out:
            out.append(cleaned)
    return out[:_MAX_CONSTRAINTS]


def _json_str_list(values: Any) -> list[str]:
    if not isinstance(values, list):
        return []
    out: list[str] = []
    for value in values:
        text = str(value)
        if text and text not in out:
            out.append(text)
    return out


def _merge_unique(existing: list[str], additions: list[str], *, limit: int) -> list[str]:
    out: list[str] = []
    for value in existing + additions:
        if value and value not in out:
            out.append(value)
        if len(out) >= limit:
            break
    return out


def _remove_values(values: list[str], removals: set[str]) -> list[str]:
    return [value for value in values if value not in removals]


def _matches_any(text: str, markers: tuple[str, ...]) -> bool:
    low = text.lower()
    return any(m in low for m in markers)


def _conversation_state_for_prompt(
    state: AskConversationState | None, items: list[WardrobeItem]
) -> dict[str, Any]:
    if state is None:
        return {}
    item_positions = {str(item.id): index + 1 for index, item in enumerate(items)}

    def _positions(ids: list[str]) -> list[int]:
        return [item_positions[i] for i in ids if i in item_positions]

    block: dict[str, Any] = {}
    if state.summary:
        summary = sanitize_llm_text(state.summary, max_len=_MAX_CONVERSATION_SUMMARY_LEN)
        if summary:
            block["summary"] = summary
    constraints = _sanitize_constraint_list(state.active_constraints or [])
    if constraints:
        block["active_constraints"] = constraints
    rejected_positions = _positions(_json_str_list(state.rejected_item_ids))
    if rejected_positions:
        block["rejected_item_positions"] = rejected_positions
    preferred_positions = _positions(_json_str_list(state.preferred_item_ids))
    if preferred_positions:
        block["preferred_item_positions"] = preferred_positions
    if isinstance(state.last_outfit, dict):
        last_positions = _positions(_json_str_list(state.last_outfit.get("item_refs")))
        if last_positions:
            block["last_outfit_positions"] = last_positions
    return block


def _heuristic_constraints(
    *,
    question: str,
    context: dict[str, Any] | None,
    is_follow_up: bool,
    rejected_item: bool,
) -> list[str]:
    constraints: list[str] = []
    if context:
        scene = context.get("scene")
        if isinstance(scene, str):
            cleaned = sanitize_llm_text(scene, max_len=60)
            if cleaned:
                constraints.append(f"scene:{cleaned}")
        minutes = context.get("time_left_minutes")
        if isinstance(minutes, int):
            constraints.append(f"time_left_minutes:{minutes}")
    if rejected_item:
        constraints.append("exclude focused item")
    elif is_follow_up and _matches_any(question, _REFINEMENT_MARKERS):
        cleaned = sanitize_llm_text(question, max_len=_MAX_CONSTRAINT_LEN)
        if cleaned:
            constraints.append(cleaned)
    return _merge_unique([], constraints, limit=_MAX_CONSTRAINTS)


def _summarize_old_messages(
    previous_summary: str | None, messages: list[AskMessage]
) -> str | None:
    if not messages and not previous_summary:
        return None
    parts: list[str] = []
    if previous_summary:
        cleaned_previous = sanitize_llm_text(
            previous_summary, max_len=_MAX_CONVERSATION_SUMMARY_LEN // 2
        )
        if cleaned_previous:
            parts.append(cleaned_previous)
    for message in messages:
        content = sanitize_llm_text(message.content, max_len=90)
        if content:
            parts.append(f"{message.role}:{content}")
    summary = " | ".join(parts)
    return summary[:_MAX_CONVERSATION_SUMMARY_LEN] or None


async def answer_question(
    session: AsyncSession,
    user_id: uuid.UUID,
    *,
    question: str,
    context: dict[str, Any] | None,
    llm: LlmPort,
    locale: str | None = None,
    focused_item_id: uuid.UUID | None = None,
    conversation_id: uuid.UUID | None = None,
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
    sanitized_question = sanitize_llm_text(question)

    settings = get_settings()
    # EXPOSURE gate: when ENABLE_ASK_CONVERSATIONS is OFF, a supplied
    # conversation_id must NOT alter the answer context. Rows still persist
    # (clean rollback), but prompt-context injection + the durable-rejection
    # pool exclusion below are gated on the flag, so flag-off + a known
    # conversation_id yields the same answer context as no conversation_id.
    conversations_enabled = settings.ENABLE_ASK_CONVERSATIONS

    conversation_repo = SqlAlchemyAskConversationRepository(session)
    if conversation_id is not None:
        conversation = await conversation_repo.get_owned_conversation(user_id, conversation_id)
        if conversation is None:
            raise FriendlyException(
                error_codes.NOT_FOUND,
                "Conversation not found",
                status_code=404,
            )
    else:
        conversation = await conversation_repo.create_conversation(
            user_id=user_id,
            title=sanitize_llm_text(question, max_len=80) or None,
        )
    conversation_state = await conversation_repo.get_state(user_id, conversation.id)
    # Rejection is derived from the STRUCTURED model output post-call, never from
    # substring-sniffing free text (that misfired on phrases like "不要太正式").
    # The DURABLE rejected_item_ids (persisted from a prior turn's structured
    # signal) drive same-turn pool exclusion below ONLY when conversations are
    # enabled. Computed once regardless (so the merge-back below stays correct).
    rejected_item_ids = _json_str_list(
        conversation_state.rejected_item_ids if conversation_state else None
    )
    existing_rejected_ids = set(rejected_item_ids)

    # Priority order: most-worn / most-recent first. NULLs-last keeps never-worn
    # items from sorting to the top under DESC. Reused for the per-category
    # ranking and the final pool order the curator buckets from.
    priority_order = (
        WardrobeItem.wear_count.desc(),
        nulls_last(WardrobeItem.last_worn_at.desc()),
        WardrobeItem.updated_at.desc(),
        WardrobeItem.id,
    )
    # Rank within each category, then keep the top `_PER_CATEGORY_FETCH_CAP` per
    # category — so a huge single-category-dominated wardrobe can't truncate
    # other categories out of the pool before curation balances coverage.
    ranked = (
        select(
            WardrobeItem.id,
            func.row_number()
            .over(partition_by=WardrobeItem.category, order_by=priority_order)
            .label("rn"),
        )
        .where(
            WardrobeItem.user_id == user_id,
            WardrobeItem.status == ItemStatus.WEARABLE.value,
            WardrobeItem.deleted_at.is_(None),
        )
        .subquery()
    )
    pool = list(
        await session.scalars(
            select(WardrobeItem)
            .join(ranked, WardrobeItem.id == ranked.c.id)
            .where(ranked.c.rn <= _PER_CATEGORY_FETCH_CAP)
            .order_by(*priority_order)
        )
    )
    if conversations_enabled and existing_rejected_ids:
        pool = [item for item in pool if str(item.id) not in existing_rejected_ids]
    # Curate to a stable, explainable set; the focused item (if any) is pinned
    # first even when it's not wearable or fell outside the fetch cap. It is
    # always shown this turn — whether the user rejects it is only known after
    # the call, from the structured signal.
    items = _curate_candidates(pool, focused_item, max_items=_MAX_ITEMS)
    today = datetime.now(UTC).date()

    # Quota — refuses BEFORE any LLM spend (the item fetch above is a cheap DB
    # read, not spend). Projecting from the assembled candidate count keeps the
    # reservation honest as the prompt grows. DB-aggregate path (no
    # redis_client) so tests are deterministic; no-op when ENABLE_COST_GUARD off.
    await assert_within_quota(session, user_id, _JOB_TYPE, cost_tokens=_project_tokens(len(items)))

    structured = settings.ENABLE_ASK_STRUCTURED_OUTPUT
    # BE#46 — durable preference memory, read only when the flag is on. Rows are
    # written regardless of the flag; gating the READ keeps a flip a clean
    # rollback. Values are bounded at write time, so they go straight into the
    # context block built above.
    preference_memory: dict[str, list[dict[str, Any]]] = {}
    if settings.ENABLE_PREFERENCE_MEMORY:
        preference_memory = await read_preference_memory(
            SqlAlchemyPreferenceMemoryRepository(session), user_id
        )
    # BE#30 — when the request omits a locale, fall back to the user's
    # persisted settings locale (then the zh-CN default) so the answer
    # language tracks the user's UI choice.
    settings_locale = await session.scalar(select(UserSettings.locale).where(UserSettings.user_id == user_id))
    effective_locale = resolve_locale(locale, settings_locale)
    ctx = await build_wardrobe_context(
        session,
        user_id,
        items=items,
        focused_item_id=focused_item.id if focused_item is not None else None,
        locale=_resolve_lang(locale, settings_locale),
        today=today,
        candidate_selection=_candidate_selection_meta(items, focused_item),
        preference_memory=preference_memory,
        # Inject prior conversation state into the prompt ONLY when the feature
        # is on; flag-off keeps the prompt context independent of conversation_id.
        conversation_state=(
            _conversation_state_for_prompt(conversation_state, items)
            if conversations_enabled
            else {}
        ),
    )
    system_prompt = _build_system_prompt(user_id, items, ctx, today, structured=structured)
    answer = await llm.ask(system_prompt, sanitized_question or question, json_mode=structured)

    if not answer.text:
        # Provider not configured (LLM_PROVIDER=none) — degrade gracefully.
        raise FriendlyException(
            error_codes.INTERNAL_ERROR,
            "Ask is not available",
            status_code=503,
        )

    answer_text = answer.text
    item_refs: list[str]
    # Preferences the user explicitly stated this turn — only the structured
    # path can surface them reliably; empty everywhere else.
    stated_preferences: list[dict[str, str]] = []
    # Rejection of the focused item comes ONLY from the structured channel; with
    # structured off / parse failed we leave durable rejection untouched rather
    # than guess from substrings.
    focus_is_rejection = False
    trip_intent: TripIntent | None = None
    if structured:
        # Parse the JSON envelope exactly once; feed both the item/preference
        # extractor and the conversation-deltas extractor from the same dict.
        try:
            parsed_data = json.loads(answer.text)
        except ValueError:  # JSONDecodeError is a ValueError subclass
            parsed_data = None
        parsed = (
            _extract_structured(parsed_data, len(items))
            if isinstance(parsed_data, dict)
            else None
        )
        if parsed is None:
            # The model ignored the JSON contract (distinct from a provider
            # error, which surfaces above as empty text). Stay honest: keep the
            # raw text as the answer but fall back to the focused item only.
            logger.warning("ask.structured_output.parse_failed")
            item_refs = [str(focused_item.id)] if focused_item is not None else []
            parsed_deltas = ConversationDeltas(reasoning=None, applied_constraints=[])
        else:
            answer_text, indices, stated_preferences = parsed
            referenced_items = [items[i] for i in indices]
            item_refs = [str(it.id) for it in referenced_items]
            parsed_deltas = _extract_conversation_deltas(cast(dict[str, Any], parsed_data))
            trip_intent = _extract_trip_intent(cast(dict[str, Any], parsed_data))
            # BE#66 — deterministic self-critique: surface (do NOT mutate) any
            # cited item that contradicts an explicit negative preference, so the
            # regression is observable without risking suppressing a valid answer.
            negatives = preference_memory.get("negative") or []
            if negatives:
                violations = negative_preference_violations(
                    [
                        {
                            "id": str(it.id),
                            "color_main": it.color_main,
                            "material": it.material,
                            "fit_status": it.fit_status,
                            "sub_category": it.sub_category,
                        }
                        for it in referenced_items
                    ],
                    negatives,
                )
                if violations:
                    logger.warning("ask.self_critique.negative_preference", violations=violations)
        focus_is_rejection = focused_item_id is not None and parsed_deltas.rejected_focused_item
    else:
        # Structured output disabled: we can't tell which items the model
        # cited, so item_refs degrades to the focused item only (or empty).
        logger.warning("ask.structured_output.disabled", item_refs_quality="limited")
        item_refs = [str(focused_item.id)] if focused_item is not None else []
        parsed_deltas = ConversationDeltas(reasoning=None, applied_constraints=[])
    answer_text = sanitize_generated_copy(
        _strip_ordinal_leaks(answer_text),
        fallback=_ASK_FALLBACK_COPY,
    )
    tokens_used = answer.tokens_used
    heuristic_constraints = _heuristic_constraints(
        question=sanitized_question,
        context=context,
        is_follow_up=conversation_state is not None,
        rejected_item=focus_is_rejection,
    )
    response_deltas = ConversationDeltas(
        reasoning=parsed_deltas.reasoning,
        applied_constraints=_merge_unique(
            parsed_deltas.applied_constraints,
            heuristic_constraints,
            limit=_MAX_CONSTRAINTS,
        ),
    )

    # Persist the context snapshot alongside the client-supplied context
    # (date/scene/time_left) so the answer stays traceable to the wardrobe
    # state it was grounded in.
    persisted_context = {
        **(context or {}),
        "snapshot": context_snapshot(ctx),
        "conversation": {
            "conversation_id": str(conversation.id),
            "deltas": {
                "reasoning": response_deltas.reasoning,
                "applied_constraints": response_deltas.applied_constraints,
            },
        },
    }
    if trip_intent is not None:
        # BE#120 — persist the trip signal so history can re-offer the
        # packing-list handoff; only affirmative intents are stored.
        persisted_context["trip_intent"] = {
            "is_trip": True,
            "duration_days": trip_intent.duration_days,
        }

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

    await conversation_repo.append_message(
        conversation=conversation,
        role="user",
        content=question,
        ask_session_id=ask_row.id,
        message_metadata={
            "focused_item_id": str(focused_item_id) if focused_item_id is not None else None,
            "focused_item_rejected": focus_is_rejection,
        },
    )
    await conversation_repo.append_message(
        conversation=conversation,
        role="assistant",
        content=answer_text,
        ask_session_id=ask_row.id,
        message_metadata={"item_refs": item_refs},
    )

    existing_constraints = _sanitize_constraint_list(
        conversation_state.active_constraints if conversation_state else []
    )
    active_constraints = _merge_unique(
        existing_constraints,
        response_deltas.applied_constraints,
        limit=_MAX_CONSTRAINTS,
    )
    preferred_item_ids = _json_str_list(conversation_state.preferred_item_ids if conversation_state else None)
    if focused_item_id is not None:
        focused_id_text = str(focused_item_id)
        if focus_is_rejection:
            rejected_item_ids = _merge_unique(rejected_item_ids, [focused_id_text], limit=200)
            preferred_item_ids = _remove_values(preferred_item_ids, {focused_id_text})
        else:
            preferred_item_ids = _merge_unique(preferred_item_ids, [focused_id_text], limit=200)
            rejected_item_ids = _remove_values(rejected_item_ids, {focused_id_text})
    summary = conversation_state.summary if conversation_state is not None else None
    if conversation.message_count > _CONVERSATION_SUMMARY_THRESHOLD:
        messages = await conversation_repo.list_messages(
            user_id, conversation.id, limit=_MAX_MESSAGES_TO_SUMMARIZE
        )
        summary = _summarize_old_messages(
            summary,
            messages[:-_RECENT_MESSAGES_TO_KEEP],
        )
    await conversation_repo.upsert_state(
        conversation=conversation,
        state=conversation_state,
        last_outfit={
            "item_refs": item_refs,
            "answer": sanitize_llm_text(answer_text, max_len=240),
        },
        active_constraints=active_constraints,
        rejected_item_ids=rejected_item_ids,
        preferred_item_ids=preferred_item_ids,
        summary=summary,
    )

    # BE#46 — trusted write path: persist preferences the user EXPLICITLY stated
    # this turn. Written unconditionally (the ENABLE_PREFERENCE_MEMORY flag gates
    # only the READ), so flipping the flag on later already has data — and a flip
    # back stays a clean rollback. Fail-soft per row: a bad type/value (the write
    # service re-validates against its allowlist) is dropped, never fails the
    # answer the user actually asked for. `ask_row.id` is the provenance link.
    if stated_preferences:
        pref_repo = SqlAlchemyPreferenceMemoryRepository(session)
        for pref in stated_preferences:
            # BE#87 — drop any preference whose value contains forbidden copy
            # (body-shaming / age-anxiety language). Dropping is honest;
            # rewriting could reverse the user's stated meaning.
            pref_value = pref.get("value", "")
            if find_forbidden_terms(pref_value):
                logger.warning(
                    "copy_guard.preference_dropped",
                    preference_type=pref.get("type"),
                )
                continue
            try:
                await record_preference(
                    pref_repo,
                    user_id=user_id,
                    preference_type=pref["type"],
                    preference_value=pref_value,
                    polarity=pref["polarity"],
                    confidence=_EXPLICIT_PREFERENCE_CONFIDENCE,
                    source=SOURCE_EXPLICIT,
                    source_message_id=ask_row.id,
                )
            except PreferenceMemoryError as exc:
                logger.warning("ask.preference_write_skipped", reason=str(exc))

    await session.flush()

    return AskResult(
        ask_session_id=ask_row.id,
        conversation_id=conversation.id,
        answer=answer_text,
        item_refs=item_refs,
        has_wardrobe_context=bool(items),
        tokens_used=tokens_used,
        deltas=response_deltas,
        effective_locale=effective_locale,
        trip_intent=trip_intent,
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
