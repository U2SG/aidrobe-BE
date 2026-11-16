"""Bounded designer state shared by API input, tools, and persisted versions."""

from __future__ import annotations

import uuid
from datetime import date
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator


class PreferenceOverride(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preference_id: uuid.UUID | None = None
    type: str | None = Field(default=None, max_length=40)
    value: str | None = Field(default=None, max_length=120)

    @model_validator(mode="after")
    def specific(self) -> Self:
        if self.preference_id is None and not (self.type and self.value):
            raise ValueError("A preference override must identify one preference")
        return self


class StylingConstraints(BaseModel):
    model_config = ConfigDict(extra="forbid")
    inspiration_id: uuid.UUID | None = Field(default=None, exclude_if=lambda value: value is None)
    locked_item_ids: list[uuid.UUID] = Field(default_factory=list, max_length=8)
    excluded_item_ids: list[uuid.UUID] = Field(default_factory=list, max_length=100)
    goal: str | None = Field(default=None, max_length=600)
    notes: list[str] = Field(default_factory=list, max_length=8)
    target_style: str | None = Field(default=None, max_length=120)
    exploration: bool = False
    preference_overrides: list[PreferenceOverride] = Field(default_factory=list, max_length=8)
    scene: Literal["commute", "formal", "casual", "date", "travel"] | None = None
    target_date: date | None = None

    @model_validator(mode="after")
    def consistent(self) -> Self:
        if set(self.locked_item_ids) & set(self.excluded_item_ids):
            raise ValueError("A piece cannot be both locked and excluded")
        if any(len(note) > 240 for note in self.notes):
            raise ValueError("A design note is too long")
        self.locked_item_ids = list(dict.fromkeys(self.locked_item_ids))
        self.excluded_item_ids = list(dict.fromkeys(self.excluded_item_ids))
        return self


class DesignBrief(BaseModel):
    """Current accepted conditions, including turns without a complete outfit."""

    revision: int
    constraints: StylingConstraints
    needs_outfit_update: bool
