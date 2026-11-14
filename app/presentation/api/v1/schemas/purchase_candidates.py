"""V3-004 — purchase candidate request/response models (contract §5.2).

V3 surfaces return data directly rather than through the `{code, message, data}`
envelope (the insights / outfit-history / outfit-visual convention recorded in
`CLAUDE.md`); errors still use the envelope via the exception middleware.
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel, Field

# Literals rather than the StrEnum so OpenAPI emits an inline enum FE can read
# without a component indirection, matching the frozen contract's jsonc block.
SourceType = Literal["share_link", "screenshot", "order_image", "ask_link"]
# `submitted` is part of the frozen enum but is NOT reachable through this
# endpoint: parsing is synchronous, so a created row is already terminal. It
# stays here so a later async parse is an additive change and so a client that
# does poll handles the value. Contract §5.2 records the same thing.
CandidateStatus = Literal["submitted", "parsed", "parse_failed"]


class ParsedItemModel(BaseModel):
    """Frozen minimal shape (contract §5.2) — every field nullable.

    `null` means "not determined", which is distinct from an empty list. Values
    come from our own bounded vocabulary (see
    `app/application/purchase/candidate_normalizer.py`), never verbatim from the
    product page, so this object is safe to put in an LLM prompt.
    """

    category: str | None = None
    colors: list[str] | None = None
    silhouette: str | None = None
    # Coarse garment type (tee, turtleneck, jeans, sneakers, …); additive to
    # the frozen §5.2 shape. Duplicate checks require it to match when known.
    garment_type: str | None = None
    material_hints: list[str] | None = None
    price: str | None = None
    scene_tags: list[str] | None = None


class CreatePurchaseCandidateRequest(BaseModel):
    source_type: SourceType = Field(
        description="Where the candidate came from. `share_link`/`ask_link` require "
        "`source_url`; `screenshot`/`order_image` require `source_asset_id`.",
    )
    source_url: str | None = Field(
        default=None,
        max_length=1024,
        description="Durable product link, for `share_link` / `ask_link`.",
    )
    source_asset_id: str | None = Field(
        default=None,
        max_length=512,
        description=(
            "For `screenshot` / `order_image`: the stable `object_key` returned by "
            "`POST /v1/uploads/presign` (e.g. `users/<user_id>/<uuid>.jpg`), NOT a "
            "UUID and NOT the TTL-bound `file_url`. This service has no generic "
            "asset table, so the object key IS the asset reference; it must be "
            "under the caller's own `users/<user_id>/` prefix."
        ),
    )


class PurchaseCandidateResponse(BaseModel):
    """`{id, status, parsed_item}` — contract §5.2.

    `parse_failed` is terminal but still decidable: the decision endpoint
    (V3-007) then answers `insufficient_information` rather than erroring, and
    `parsed_item` still carries whatever partial attributes were recovered so
    the client can display and edit them.
    """

    id: uuid.UUID
    status: CandidateStatus
    parsed_item: ParsedItemModel | None = None
