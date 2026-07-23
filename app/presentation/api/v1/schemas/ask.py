"""Ask Aidrobe schemas — M15."""

import uuid
from datetime import datetime

from pydantic import BaseModel, Field


class AskContext(BaseModel):
    date: str | None = None
    scene: str | None = None
    time_left_minutes: int | None = None


class AskRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    context: AskContext | None = None
    # BCP-47-ish locale hint for the answer language (e.g. "zh-CN", "en").
    # Request-passed only — not persisted on the user. Defaults to Chinese.
    locale: str | None = Field(default=None, max_length=16)
    # Scope the Ask to one wardrobe item ("style an outfit around this").
    # The item is pinned first in the candidate set; 404 if not the caller's.
    focused_item_id: uuid.UUID | None = None
    # Optional thread id for multi-turn outfit refinement. Persisted even when
    # response exposure is feature-flagged off.
    conversation_id: uuid.UUID | None = None


class AskItemRefSnapshot(BaseModel):
    """BE#29 — denormalized card for a referenced wardrobe item so the client
    can render related items without a per-id detail fetch. Only the current
    user's accessible, non-deleted items appear; `thumbnail_url` is signed at
    request time (never a persisted TTL-bound URL)."""

    item_id: uuid.UUID
    # Localized display name (falls back to the user-visible title).
    name: str
    category: str
    color_main: str
    thumbnail_url: str | None = None


class AskConversationDeltas(BaseModel):
    reasoning: str | None = None
    applied_constraints: list[str] = Field(default_factory=list)


class AskResponse(BaseModel):
    ask_session_id: uuid.UUID
    conversation_id: uuid.UUID | None = None
    answer: str  # LLM prose, generated in the effective locale (prompt-driven)
    item_refs: list[str]
    item_ref_snapshots: list[AskItemRefSnapshot] = Field(default_factory=list)
    # BE#60 — true when the answer was grounded in wardrobe context provided to
    # the prompt. The prompt MUST NOT claim "无法查看衣橱" when this is true.
    has_wardrobe_context: bool = False
    locale: str = "zh-CN"  # effective resolved locale echoed back to the client
    tokens_used: int
    deltas: AskConversationDeltas | None = None
    # BE#86 — stable identifiers so the client can anchor feedback.
    # `recommendation_id` equals `ask_session_id` when the answer references
    # wardrobe items (item_refs non-empty); None for pure-text answers.
    # `source_surface` is a fixed literal so FE/analytics never guess the origin.
    recommendation_id: uuid.UUID | None = None
    source_surface: str = "ask"


class AskHistoryItem(BaseModel):
    ask_session_id: uuid.UUID
    # Conversation this turn belongs to (resolved via ask_messages) so the
    # client can resume it; None for legacy rows without message linking.
    conversation_id: uuid.UUID | None = None
    question: str
    answer: str | None = None
    item_refs: list[str] = Field(default_factory=list)
    item_ref_snapshots: list[AskItemRefSnapshot] = Field(default_factory=list)
    created_at: datetime
