"""V3-016 — the provider boundary for static try-on previews.

`StaticTryOnPort` is one of the four port names frozen by contract §6.4, so the
name is not a local choice. MiniMax or any other vendor is an adapter behind it
and "never leak[s] into the domain contract or API responses" — which is why
`provider` / `model_version` / `prompt_version` are returned as *data* here and
stored as internal columns on `outfit_visual_assets`, rather than being chosen
by the caller or surfaced to FE.

**The identity lives on the port, and that is the one place V3-016 deliberately
departs from V3-013.** `prepare_avatar.preparation_pipeline_identity()` mirrors
`container.avatar_preparation()` by importing the fake adapter's constants into
the application layer, and pins the two together with a unit test. That shape
costs two things here that it did not cost there:

1. V3-016 acceptance 1 is literally "Application layer depends only on a
   StaticTryOnPort". A module under `app/application/` importing
   `app.infrastructure.vision.fake_static_tryon` — even inside a function body —
   is exactly the counterexample a leakage review looks for.
2. A mirror can drift, and this codebase has already paid for that twice: V3-012
   recorded that "a key that claims an assessment the worker did not make makes
   rows permanently unreachable", and V3-013 added a pinning test to patch over
   the same shape.

So the port answers `pipeline_identity` itself. V3-017 needs that triple *before*
any generation happens — contract §6.3's strong cache key is
`(avatar_version_id, normalized_candidate_asset_hash, sorted_supporting_item_ids,
view_type, provider_model_version, prompt_version)`, i.e. the key is composed at
enqueue time, not from a result — and it gets it by asking the adapter the
container selected. There is nothing to mirror and nothing to pin, because the
pipeline that reports the identity is the pipeline that will answer the job.
`StaticTryOnResult.identity` closes the loop from the other end: a result carries
the identity of whatever actually ran, so a test can assert the two are equal
instead of trusting a convention.

**Nothing in this module can describe a body.** That is contract §7 and V3_SPEC
§15 expressed as a type, the same move V3-012 made with `ports/avatar_detection.py`
and V3-013 with `ports/avatar_preparation.py`. `StaticTryOnResult` carries image
bytes and three provenance strings and has no field a measurement, size, age,
weight, body-shape judgement or fidelity score could be written into. An adapter
that wanted to return "estimated size M" or `minimax_task_id` would have nowhere
to put it without editing this file, which is a review event rather than a silent
addition — that is what makes acceptance 4 structural at the boundary and not
only asserted at the response schema.

**Bytes in, bytes out — never keys and never URLs.** `StaticTryOnRequest` carries
image bytes for the same reason `AvatarPreparationPort.prepare` does: the port
must not be able to reach into storage on its own, so the caller stays the only
place that decides which object may be read, and no signed URL is ever handed to
a provider adapter (contract §7).

**Development and CI use fake/recorded adapters only** (contract §6.4, and the
loop's safety rule that real user photos are never sent to a third-party provider
during development). `FakeStaticTryOn` is the only implementation in the tree;
production provider selection is this feature's explicit human gate and is out of
scope for P0.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class PoseTemplate(StrEnum):
    """The fixed templates a preview may be rendered against (V3_SPEC §10.3).

    One value in P0, and that is the product decision rather than a stub: the
    generation request is a *fixed* template so a preview cannot become an
    open-ended styling editor (contract §9, "Infinite try-on / outfit editor" is
    an explicit exclusion — "One preview per decision; BE picks supporting items;
    no FE styling submission").
    """

    NEUTRAL_FULL_BODY = "neutral_full_body"


class ViewRequirement(StrEnum):
    """Which view a render must produce.

    `front` only, and the absence of the others is an acceptance criterion, not
    an omission: contract §9 excludes multi-view / 360° / video from P0 and
    reserves `MultiViewGenerationPort` for P1. A second member here would be the
    first step of that drift, so there isn't one.
    """

    FRONT = "front"


class GarmentSlot(StrEnum):
    """Where a garment sits on the body.

    Same four slots V3-006 composes outfits with (`outfit_composition.FILL_ORDER`).
    Restated here rather than imported so a port stays free of application
    dependencies; `tests/unit/test_v3_static_tryon_port.py` asserts the two sets
    are equal, so a slot added there without one here is a red test rather than a
    garment the renderer silently drops.

    A slot is a position, never a body description — `torso` says where a shirt
    goes, not anything about the person wearing it.
    """

    TORSO = "torso"
    LEGS = "legs"
    FEET = "feet"
    OUTER = "outer"


@dataclass(frozen=True)
class GarmentImage:
    """One garment to render, and where it belongs."""

    image: bytes
    slot: GarmentSlot


@dataclass(frozen=True)
class StaticTryOnRequest:
    """Everything a provider is given, and nothing else.

    Mirrors V3_SPEC §10.3's generation request with asset ids resolved to bytes
    by the caller. The candidate is a separate field from the supporting items
    because they are different things to the product — the candidate is the
    garment the purchase decision is *about* — and because a renderer that could
    not tell them apart could quietly drop the one item the preview exists for.

    Deliberately absent: any user identifier, any wardrobe metadata, any decision
    text, the concern the user raised, and any free-text prompt. A provider gets
    pixels, a slot per garment and two frozen template values. That keeps
    contract §7's "no body / identity inference" boundary at the type level
    rather than at the log line.
    """

    avatar_image: bytes
    candidate_garment: GarmentImage
    supporting_garments: tuple[GarmentImage, ...]
    pose_template: PoseTemplate
    view: ViewRequirement


@dataclass(frozen=True)
class PipelineIdentity:
    """Which provider pipeline is answering, as three internal strings.

    Internal by contract §6.4 — these reach `outfit_visual_assets` columns and
    operator logs, never a response shape.
    """

    provider: str
    model_version: str
    prompt_version: str

    def as_slug(self) -> str:
        """`provider:model:prompt`, for cache-key and idempotency-key composition.

        Callers are expected to *digest* this rather than embed it, the lesson
        `prepare_avatar._IDENTITY_DIGEST_CHARS` records: a realistic vendor triple
        (`minimax-video-01:<dated-model>:tryon-prompt-v12`) is long enough to blow
        a `String(128)` key budget, and contract §6.3 already specifies
        `static_tryon:{sha256(cache_key)}` for exactly that reason.
        """
        return f"{self.provider}:{self.model_version}:{self.prompt_version}"


@dataclass(frozen=True)
class StaticTryOnResult:
    """One rendered preview and the pipeline that produced it.

    `identity` rather than three loose strings, so "what ran" is one value that
    can be compared to `StaticTryOnPort.pipeline_identity` and written to the
    asset row without being reassembled field by field.

    No quality score, no confidence, no fidelity signal: whether a render may be
    shown is V3-018's `VisualQualityGatePort` decision over the image, not
    something the generating vendor gets to assert about its own output.
    """

    image: bytes
    identity: PipelineIdentity


class StaticTryOnPort(Protocol):
    @property
    def pipeline_identity(self) -> PipelineIdentity:
        """The provider pipeline this adapter *is*, knowable without rendering.

        Contract §6.3 puts `provider_model_version` and `prompt_version` in the
        strong cache key, and a cache key that could only be computed after
        generation would never prevent a paid call — so this is a property, not a
        field of the result alone.
        """
        ...

    async def render(self, request: StaticTryOnRequest) -> StaticTryOnResult:
        """Render one static full-outfit preview.

        Raises on failure. There is no partial-success return shape: a half
        rendered preview is not something that may become `ready`, and the
        distinction between a technical failure and an unshowable result is
        V3-018's (`failed` vs `rejected_by_quality_gate`), decided by the quality
        gate rather than reported by the vendor.
        """
        ...
