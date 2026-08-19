"""V3-019 — request/response schemas for the static visual preview lifecycle.

The request body is contract §6.1's, frozen verbatim:
``{"view_type": "static_tryon", "concern": "volume|color|length|overall",
"consent": true}``. Three deliberate choices, one per field:

- ``view_type`` is ``Literal["static_tryon"]`` because P0 has exactly one view
  type; a second one changes the §6.3 cache key's components, not just this
  schema (see ``request_static_preview.VIEW_TYPE``).
- ``concern`` is the §6.1 enum. It is NOT §5.3's decision-concern enum —
  V3-001's evaluator flagged the pair as two frozen enums that must not be
  unified. The server does not consume it: the cache key excludes it by
  contract (identical inputs never regenerate, whatever the stated concern) and
  the rule engine that would grade it is FE display territory (V3-020). It is
  validated here so a typo'd client fails loudly (422) instead of silently
  requesting something the contract does not define.
- ``consent`` is ``Literal[True]``: the §6.1 body freezes the value, not just
  the field. This is the per-request confirmation that THIS render of the
  user's likeness was asked for — V3-013's avatar-level consent authorizes
  keeping the reference, not any particular use of it (the V3-017 handoff says
  exactly this). ``consent: false`` is therefore not a state to route on but a
  malformed request: 422, the V3-007 precedent for out-of-vocabulary enum
  values. The accepted ``true`` is persisted into the asset's
  ``input_snapshot`` so the attestation outlives the request.

The success response is §6.1's frozen pair. A cache hit returns the existing
asset with whatever ``generation_status`` it already reached (possibly
``ready``); a fresh request returns ``queued``. The client cannot tell a
revived retry from a fresh enqueue, and does not need to — both mean "poll".

V3-024 adds §6.1's feedback pair at the bottom of this file; its own reasoning
is in `PreviewFeedbackRequest`.
"""

import uuid
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field

#: Contract §6.1's preview-concern vocabulary — distinct from §5.3's.
PreviewConcern = Literal["volume", "color", "length", "overall"]

#: Contract §6.1's frozen feedback vocabularies. Spelled as `Literal`s rather
#: than built from `PreviewFeedbackVerdict` / `PreviewFeedbackAspect` so the
#: generated OpenAPI carries a plain string enum FE can read without a
#: component indirection — `tests/unit/test_v3_preview_feedback_contract.py`
#: asserts each one still equals its enum, so the convenience cannot drift.
PreviewVerdict = Literal["looks_like_me", "not_like_me", "garment_wrong"]
PreviewAspect = Literal["face", "body_ratio", "garment_fit", "color", "other"]


class VisualPreviewRequest(BaseModel):
    # `extra="forbid"` is V3-019 acceptance 2 made structural: the frozen body
    # has exactly these three fields, so a client smuggling outfit-editor state
    # (item lists, slots, styling knobs) is rejected with 422 instead of having
    # the extras silently dropped — the server composes the outfit from the
    # decision snapshot and nothing the client sends can influence it.
    model_config = {"extra": "forbid"}

    view_type: Literal["static_tryon"]
    concern: PreviewConcern
    consent: Literal[True]


class VisualPreviewResponse(BaseModel):
    asset_id: uuid.UUID
    generation_status: str


class PreviewFeedbackRequest(BaseModel):
    """Contract §6.1's frozen feedback body, verbatim.

    ``{"verdict": "looks_like_me|not_like_me|garment_wrong", "aspects": [...]}``
    with ``aspects`` optional. V3-020 sends `{"verdict": "…"}` with the key
    **absent**, and that is the shape this must keep accepting.

    ``extra="forbid"`` is the V3-019 precedent applied to the same threat: the
    contract froze two fields, so a client attaching a comment, a score or a
    provider hint is rejected with 422 rather than having it silently dropped.
    It matters more here than on the create surface, because this endpoint is
    the one place a free-text field would be tempting — and
    docs/v3-analytics-events.md §5 rules free text out of this signal entirely.

    ``aspects`` is capped at the size of its own vocabulary: five distinct
    values exist, so a longer list is either duplicates (normalized away
    server-side) or an attempt to make the field carry volume. Duplicates are
    accepted and deduplicated rather than rejected — the client is describing
    one render, not submitting a set.
    """

    model_config = {"extra": "forbid"}

    verdict: PreviewVerdict
    aspects: list[PreviewAspect] | None = Field(default=None, max_length=5)


class PreviewFeedbackResponse(BaseModel):
    """What was recorded, echoed back — and nothing else.

    Contract §6.1 froze the request body but no response shape, so this is the
    minimum that lets a client confirm the stored state after a conflicting
    resubmission (`verdict` is the *stored* one, which is the submitted one
    under last-write-wins). It carries no `provider`, `model_version` or
    `prompt_version`: §6.4 keeps that provenance internal, and this endpoint's
    whole purpose is feeding an evaluation the user must not be able to read
    the pipeline identity out of.

    `recorded_at` is the row's `updated_at`, so an identical resubmission
    returns the *original* timestamp — the observable difference between a
    no-op and a change of mind.
    """

    asset_id: uuid.UUID
    verdict: PreviewVerdict
    aspects: list[PreviewAspect] | None = None
    recorded_at: datetime
