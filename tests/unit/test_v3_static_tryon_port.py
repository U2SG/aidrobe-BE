"""V3-016 — the `StaticTryOnPort` contract itself.

Acceptance 4 says vendor-specific fields must not leak into the public API, and
the strongest version of that is not a response-schema assertion — it is that a
vendor has nowhere to *put* one. These tests read the frozen shapes and refuse
anything a provider could smuggle through: an extra result field, a body or
fidelity claim, a second view, a second pose template.

Container-free and network-free by construction, so `--noconftest` runs the whole
file as local evidence.
"""

from __future__ import annotations

import dataclasses

from app.application.ports.static_tryon import (
    GarmentImage,
    GarmentSlot,
    PipelineIdentity,
    PoseTemplate,
    StaticTryOnPort,
    StaticTryOnRequest,
    StaticTryOnResult,
    ViewRequirement,
)
from app.application.purchase.outfit_composition import FILL_ORDER
from app.infrastructure.vision.fake_static_tryon import FakeStaticTryOn

#: Language a preview may never carry, in any field name, on either side of the
#: port. Same instrument as V3-007's `BANNED_CLAIM_TERMS`, applied to the type
#: rather than to rendered copy: contract §7 forbids size/fit claims and body or
#: identity inference, and V3-016's own acceptance 4 forbids vendor detail.
#:
#: Matched against `_`-separated **tokens**, not as substrings, and that is
#: load-bearing rather than fussy: `image` contains "age" and `avatar_image`
#: contains it twice, so a substring test would reject the two fields the port
#: exists to carry and would have to be weakened until it caught nothing. Tokens
#: mean `body_shape` fails and `image` passes.
BANNED_FIELD_TOKENS = (
    # body / identity inference (contract §7, V3_SPEC §15)
    "body",
    "size",
    "fit",
    "measure",
    "height",
    "weight",
    "age",
    "gender",
    "skin",
    "face",
    "shape",
    "attractive",
    # a vendor asserting the quality of its own output — V3-018's call, not the
    # generating provider's
    "score",
    "confidence",
    "fidelity",
    "quality",
    # vendor-specific plumbing
    "raw",
    "seed",
    "task",
    "endpoint",
    "api",
    "vendor",
    "minimax",
)

PORT_SHAPES = (GarmentImage, StaticTryOnRequest, PipelineIdentity, StaticTryOnResult)


def _field_names(shape: type) -> set[str]:
    return {field.name for field in dataclasses.fields(shape)}


def test_a_result_carries_only_pixels_and_a_pipeline_identity() -> None:
    """The frozen result shape, asserted exactly rather than as a subset.

    An adapter that wanted to return `minimax_task_id`, `seed` or "estimated size
    M" has nowhere to put it without editing `ports/static_tryon.py` — which is a
    review event. That is what makes acceptance 4 structural at the boundary and
    not only checked at the response schema.
    """
    assert _field_names(StaticTryOnResult) == {"image", "identity"}


def test_a_pipeline_identity_is_exactly_the_three_internal_strings() -> None:
    """`provider` / `model_version` / `prompt_version` — V3_SPEC §11.3's triple.

    Exactly three, so a fourth provenance field cannot appear on asset rows
    without this test failing first.
    """
    assert _field_names(PipelineIdentity) == {"provider", "model_version", "prompt_version"}


def test_a_request_carries_pixels_slots_and_two_frozen_template_values() -> None:
    """What a provider is given — and, by exclusion, what it is never given.

    No user id, no wardrobe metadata, no decision text, no concern, no free-text
    prompt. Contract §7's "no body / identity inference" boundary held at the
    type level rather than at the log line.
    """
    assert _field_names(StaticTryOnRequest) == {
        "avatar_image",
        "candidate_garment",
        "supporting_garments",
        "pose_template",
        "view",
    }


def test_no_field_on_either_side_of_the_port_can_hold_a_claim() -> None:
    for shape in PORT_SHAPES:
        for name in _field_names(shape):
            tokens = set(name.split("_"))
            offending = tokens.intersection(BANNED_FIELD_TOKENS)
            assert not offending, f"{shape.__name__}.{name} contains banned token {offending}"


def test_the_banned_token_list_is_not_vacuous() -> None:
    """The deny-list must be able to fail, or it asserts nothing.

    Token matching is narrow enough to be worth proving on a positive case: the
    exact fields a provider would want to add — `body_shape`, `estimated_size`,
    `minimax_task_id` — must all be caught.
    """
    for smuggled in ("body_shape", "estimated_size", "fit_score", "minimax_task_id", "raw_response"):
        assert set(smuggled.split("_")).intersection(BANNED_FIELD_TOKENS), smuggled


def test_p0_renders_one_view_from_one_pose_template() -> None:
    """The P0 exclusions, as enum cardinality.

    Contract §9 excludes multi-view / 360° / video and reserves
    `MultiViewGenerationPort` for P1; it also excludes an outfit editor, which is
    why the pose template is a fixed value rather than a caller-supplied string.
    A second member of either enum is the first step of that drift, so this fails
    before it ships.
    """
    assert {member.value for member in ViewRequirement} == {"front"}
    assert {member.value for member in PoseTemplate} == {"neutral_full_body"}


def test_garment_slots_match_the_engine_that_composes_outfits() -> None:
    """`GarmentSlot` is restated, not imported — so it is asserted, not assumed.

    V3-006 decides which slots an outfit fills; this port decides what a renderer
    can be handed. Restating keeps the port free of application dependencies, and
    this equality is what stops a slot added in `outfit_composition` from
    becoming a garment the renderer silently drops.
    """
    assert {member.value for member in GarmentSlot} == set(FILL_ORDER)


def test_the_only_adapter_in_the_tree_satisfies_the_port() -> None:
    """Structural conformance, checked at runtime as well as by mypy.

    `container.static_tryon()` is annotated `-> StaticTryOnPort`, so `mypy app`
    already rejects an adapter that does not conform. This is the runtime half:
    it also pins that `pipeline_identity` is a *property* — readable without
    rendering, which is what contract §6.3's pre-generation cache key needs.
    """
    port: StaticTryOnPort = FakeStaticTryOn()

    assert isinstance(port.pipeline_identity, PipelineIdentity)
    assert callable(port.render)
    assert isinstance(FakeStaticTryOn.pipeline_identity, property)


def test_the_identity_slug_is_stable_and_ordered() -> None:
    """`provider:model:prompt`, the shape callers digest into a key.

    Pinned because `prepare_avatar` composes an idempotency key from the
    equivalent string and a reordering would silently repartition every existing
    key — the kind of change that reads as a no-op in review.
    """
    identity = PipelineIdentity(provider="p", model_version="m", prompt_version="v")

    assert identity.as_slug() == "p:m:v"


def test_shapes_are_frozen_so_a_result_cannot_be_edited_after_it_is_returned() -> None:
    """Provenance that a caller could overwrite is not provenance."""
    for shape in PORT_SHAPES:
        assert shape.__dataclass_params__.frozen, f"{shape.__name__} is mutable"


def test_a_garment_names_a_position_and_nothing_about_a_person() -> None:
    garment = GarmentImage(image=b"", slot=GarmentSlot.TORSO)

    assert _field_names(GarmentImage) == {"image", "slot"}
    assert garment.slot.value == "torso"
