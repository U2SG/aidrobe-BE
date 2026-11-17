"""V3-007 — purchase decision request/response models (contract §5.3).

V3 surfaces return data directly rather than through the `{code, message, data}`
envelope (the insights / outfit-history / outfit-visual convention recorded in
`CLAUDE.md`); errors still use the envelope via the exception middleware.

Literals rather than the `StrEnum`s so OpenAPI emits inline enums a generated
client can read without a component indirection — the V3-004 convention, and
the reason the frozen contract's jsonc block maps 1:1 onto this file. The
persistence-side CHECK constraints stay in sync with `app/common/enums.py`;
`tests/unit/test_v3_purchase_decision_rules.py` pins these literals against
those enums so the two cannot drift.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Literal, Self

from pydantic import BaseModel, Field, model_validator

from app.application.ask.design_types import StylingConstraints

Decision = Literal[
    "worth_adding",
    "consider_alternate",
    "wait",
    "avoid_duplicate",
    "insufficient_information",
]
ReasonKind = Literal[
    "redundancy",
    "compatibility",
    "scene_coverage",
    "extra_purchase_needed",
    "adoption_history",
]
Relation = Literal["covers", "complements"]
Uncertainty = Literal["low", "medium", "high"]
VisualNeed = Literal["none", "helpful", "recommended"]
NextAction = Literal["add_to_candidates", "skip", "recheck_color", "style_with_existing"]
Concern = Literal["volume", "color", "redundancy", "scene", "none"]
PurchaseChoice = Literal["buy", "skip", "later"]


class DecisionRequest(BaseModel):
    """Contract §5.3 request body. Optional — an omitted body means `none`."""

    concern: Concern = Field(
        default="none",
        description=(
            "What the user is unsure about. Only grades how prominently a visual "
            "preview is offered (`volume`/`color` are the two a static preview can "
            "answer); it never changes the decision itself."
        ),
    )
    conversation_id: uuid.UUID | None = None
    expected_revision: int | None = Field(default=None, ge=0)
    request_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def complete_design_context(self) -> Self:
        provided = (self.conversation_id, self.expected_revision, self.request_id)
        if any(value is not None for value in provided) and any(value is None for value in provided):
            raise ValueError("conversation_id, expected_revision and request_id must be provided together")
        return self


class PurchaseChoiceRequest(BaseModel):
    choice: PurchaseChoice | None


class PurchaseDesignContext(BaseModel):
    """The accepted design used by this immutable evaluation, never the live brief."""

    conversation_id: uuid.UUID
    revision: int
    constraints: StylingConstraints
    styling_revision_id: uuid.UUID | None = None
    source_message_id: uuid.UUID | None = None
    source_message_created_at: datetime | None = None
    inspiration_id: uuid.UUID | None = None
    inspiration_created_at: datetime | None = None
    inspiration_learned_at: datetime | None = None
    captured_at: datetime
    evaluated_dimensions: list[str] = Field(default_factory=list)
    unverified_dimensions: list[str] = Field(default_factory=list)


class DecisionReasonModel(BaseModel):
    """One of the 2–3 reasons, ordered by importance.

    `text` is server-rendered display copy: contract §5.3 forbids the client to
    synthesize it, and it is frozen into the decision snapshot so a historical
    explanation never changes wording. `adoption_history` is part of the frozen
    vocabulary but is not emitted in P0 (no loaded projection carries wear
    counts) — a client must still tolerate it.
    """

    kind: ReasonKind
    text: str


class ComparisonItemModel(BaseModel):
    """One row of `closet_comparison.similar_items`.

    `image_url` is a short-lived signed URL materialized at read time and never
    persisted (V3_SPEC §13.5); it is `null` when the item has no image or when
    signing is unavailable, which is a display detail rather than an error.
    """

    item_id: uuid.UUID
    image_url: str | None = None
    relation: Relation


class ClosetComparisonModel(BaseModel):
    """V3_SPEC §5.4 layer 3 — what the wardrobe already covers."""

    summary_text: str
    similar_items: list[ComparisonItemModel] = Field(default_factory=list)


class WardrobeDecisionResponse(BaseModel):
    """The frozen §5.3 `wardrobe_decision` shape.

    Nothing here depends on an avatar or a visual preview: `visual_need` only
    suggests whether a preview entry is worth showing, so the no-avatar
    experience is complete (V3_SPEC §5.5).
    """

    id: uuid.UUID
    # Required, not nullable: every P0 decision is candidate-scoped
    # (`DecidePurchase` always sets it, and the unique index keys on it).
    # Declaring it optional taught generated clients to accept a missing id
    # and would have masked a BE serialization bug (codex review, FE#136).
    candidate_id: uuid.UUID
    decision: Decision
    decision_text: str
    reasons: list[DecisionReasonModel]
    closet_comparison: ClosetComparisonModel
    uncertainty: Uncertainty
    visual_need: VisualNeed
    boundary_note: str = Field(
        description="Fixed copy: this is about the overall look, not a size or fit guarantee."
    )
    next_actions: list[NextAction]
    created_at: datetime
    design_context: PurchaseDesignContext | None = None
    choice: PurchaseChoice | None = None
    choice_updated_at: datetime | None = None
