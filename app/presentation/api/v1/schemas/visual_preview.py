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
"""

import uuid
from typing import Literal

from pydantic import BaseModel

#: Contract §6.1's preview-concern vocabulary — distinct from §5.3's.
PreviewConcern = Literal["volume", "color", "length", "overall"]


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
