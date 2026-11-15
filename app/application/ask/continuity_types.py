"""Owned, versioned context carried into an explicit shopping or travel action."""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.application.ask.design_types import StylingConstraints


class DesignContextRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: uuid.UUID
    expected_revision: int = Field(ge=0)


class DesignContextSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    conversation_id: uuid.UUID
    revision: int = Field(ge=0)
    outfit_id: uuid.UUID | None = None
    constraints: StylingConstraints


class LinkedDesignResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["purchase_decision", "packing_list"]
    resource_id: uuid.UUID
    source_revision: int = Field(ge=0)
    summary: str = Field(min_length=1, max_length=1000)
