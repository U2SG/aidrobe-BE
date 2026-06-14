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
