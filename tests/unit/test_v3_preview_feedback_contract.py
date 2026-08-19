"""V3-024 — the frozen §6.1 feedback vocabularies, and what must not leak.

Database-free. Three things a wire test cannot show as cheaply:

1. the request schema's `Literal`s are exactly the two enums, so the
   convenience spelling in `schemas/visual_preview.py` cannot drift from the
   values the CHECK constraint and the metrics query group on;
2. the literals are exactly what FE PR #149 sends, quoted from
   `aidrobe-FE/src/api/previewFeedbackMapping.ts`;
3. neither shape carries a field from the analytics contract's deny-list
   (`docs/v3-analytics-events.md` §5) — no image material, no prompt, no
   provider, no free text.
"""

from __future__ import annotations

from typing import get_args

import pytest
from pydantic import ValidationError

from app.application.purchase.record_preview_feedback import normalize_aspects
from app.common.enums import PreviewFeedbackAspect, PreviewFeedbackVerdict
from app.presentation.api.v1.schemas.visual_preview import (
    PreviewAspect,
    PreviewFeedbackRequest,
    PreviewFeedbackResponse,
    PreviewVerdict,
)

#: Copied from the FE mapping module, which is the other end of this wire.
FE_VERDICTS = ("looks_like_me", "not_like_me", "garment_wrong")
FE_ASPECTS = ("face", "body_ratio", "garment_fit", "color", "other")

#: docs/v3-analytics-events.md §5, the categories that must never appear.
DENIED_FIELDS = (
    "image_url",
    "image_bytes",
    "image_base64",
    "signed_url",
    "object_key",
    "prompt",
    "prompt_text",
    "prompt_version",
    "provider",
    "model_version",
    "quality_signals",
    "comment",
    "note",
    "free_text",
    "body_measurements",
    "body_shape_estimate",
    "height",
    "weight",
)


def test_the_verdict_literal_is_exactly_the_enum() -> None:
    assert set(get_args(PreviewVerdict)) == {member.value for member in PreviewFeedbackVerdict}


def test_the_aspect_literal_is_exactly_the_enum() -> None:
    assert set(get_args(PreviewAspect)) == {member.value for member in PreviewFeedbackAspect}


def test_the_vocabularies_are_what_the_client_already_sends() -> None:
    """Frozen means frozen: FE #149 shipped against these exact strings, so a
    rename here is a breaking change to a client that is already in review."""
    assert get_args(PreviewVerdict) == FE_VERDICTS
    assert get_args(PreviewAspect) == FE_ASPECTS


def test_the_frozen_body_parses_with_aspects_absent() -> None:
    """V3-020 omits the key rather than sending `[]`, and the difference is
    preserved all the way to the column."""
    parsed = PreviewFeedbackRequest.model_validate({"verdict": "looks_like_me"})
    assert parsed.verdict == "looks_like_me"
    assert parsed.aspects is None

    empty = PreviewFeedbackRequest.model_validate({"verdict": "looks_like_me", "aspects": []})
    assert empty.aspects == []


@pytest.mark.parametrize(
    "body",
    [
        {"verdict": "looks_great"},
        {"verdict": "looks_like_me", "aspects": ["shoulders"]},
        {"verdict": "looks_like_me", "comment": "hate it"},
        {"aspects": ["face"]},
    ],
)
def test_out_of_contract_bodies_are_rejected(body: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        PreviewFeedbackRequest.model_validate(body)


def test_aspects_cannot_be_used_as_a_volume_channel() -> None:
    """Five values exist; a longer list is duplicates or abuse."""
    with pytest.raises(ValidationError):
        PreviewFeedbackRequest.model_validate(
            {"verdict": "not_like_me", "aspects": [*FE_ASPECTS, "face"]}
        )


@pytest.mark.parametrize("shape", [PreviewFeedbackRequest, PreviewFeedbackResponse])
def test_neither_shape_carries_a_denied_field(shape: type) -> None:
    fields = set(shape.model_fields)
    assert fields & set(DENIED_FIELDS) == set()


def test_the_response_exposes_no_pipeline_provenance() -> None:
    """Contract §6.4: `provider` / `model_version` / `prompt_version` are
    internal columns. The evaluation joins them server-side; the user's own
    verdict is all that comes back."""
    assert set(PreviewFeedbackResponse.model_fields) == {
        "asset_id",
        "verdict",
        "aspects",
        "recorded_at",
    }


@pytest.mark.parametrize(
    ("given", "expected"),
    [
        (None, None),
        ([], []),
        (["face"], ["face"]),
        (["face", "color"], ["color", "face"]),
        (["face", "color", "face"], ["color", "face"]),
        (["other", "face", "color"], ["color", "face", "other"]),
    ],
)
def test_aspects_normalize_to_a_sorted_set(
    given: list[str] | None, expected: list[str] | None
) -> None:
    """Order and duplicates must not make a re-tap look like a change of mind —
    this is what keeps the `IS DISTINCT FROM` guard honest."""
    assert normalize_aspects(given) == expected


def test_normalizing_keeps_none_and_empty_apart() -> None:
    assert normalize_aspects(None) is None
    assert normalize_aspects([]) == []


def test_literal_spelling_stays_a_plain_string_enum() -> None:
    """The reason these are `Literal`s and not the `StrEnum`s: the generated
    OpenAPI carries inline string enums FE can read without a component
    indirection. Plain `str` members are what makes that true — a `StrEnum`
    member would serialize the same but emit a `$ref`."""
    for value in (*get_args(PreviewVerdict), *get_args(PreviewAspect)):
        assert type(value) is str
