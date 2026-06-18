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


class AskResponse(BaseModel):
    ask_session_id: uuid.UUID
    answer: str
    item_refs: list[str]
    tokens_used: int


class AskHistoryItem(BaseModel):
    ask_session_id: uuid.UUID
    question: str
    answer: str | None = None
    item_refs: list[str] = Field(default_factory=list)
    created_at: datetime
