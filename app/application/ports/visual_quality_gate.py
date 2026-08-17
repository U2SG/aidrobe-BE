"""V3-018 — the port a render quality checker implements.

`VisualQualityGatePort` is one of the four port names frozen by contract §6.4,
so the name is not a local choice. Whatever inspects a render — a heuristic, a
vision model, a vendor's own moderation endpoint — is an adapter behind it and
never leaks into the domain contract or API responses.

**The port measures; the rules judge.** This is the `PersonFrameDetectorPort` /
`candidate_rules` split (V3-012) applied to renders: an adapter returns a
`RenderQualityObservation` of neutral measurements, and
`app.application.purchase.visual_quality_rules` turns those into the show/hide
verdict. Keeping the verdict out of the adapter is what makes the threshold a
reviewable constant in one pure module — which matters because this feature's
human gate is exactly "final visual quality threshold requires benchmark review
before production".

**Read the field names literally, the V3-012 discipline.** Every field is a
measurement of the *rendered image file*, never an inference about the person
in it:

- `decode_ok` says the bytes decode as an image at all. A provider that
  answered 200 with truncated or non-image bytes fails here.
- `pixel_width` / `pixel_height` are the decoded dimensions.
- `tonal_range` is a normalized luminance-spread measure in [0, 1]. A blank,
  black or single-flat-color render — the classic silent provider failure —
  measures near zero. It describes the picture's tonal variation, nothing else.

There is deliberately **no field** for identity, body, garment or artifact
fidelity (V3_SPEC §14's four dimensions). None of them is derivable from the
render alone by arithmetic; determining them needs a comparison model, and
inventing a field for them would invite an adapter to claim a determination it
never made. The rules layer reports those dimensions `undetermined` all the way
into the persisted signals, where the human-gated benchmark review can see they
are still open — the same honest gap V3-012 left for orientation and content
safety.

**The request rides along for the comparison a future adapter will need.** A
real fidelity check compares the render against the avatar and garment images
it was generated from, so `observe` receives the same `StaticTryOnRequest` the
renderer got. `FakeVisualQualityGate` ignores it. Bytes, never keys or URLs —
the same reasoning as `StaticTryOnPort.render`: the port must not be able to
reach into storage, and no signed URL is ever handed to a provider adapter
(contract §7).

Raising is a legitimate answer, and the caller treats it as a **technical
failure, not a verdict** — the job retries and can end `failed`, never
`rejected_by_quality_gate`. Failing closed rather than open is the feature's
whole point: a gate outage must not mean "return every successful provider
response unchecked".
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from app.application.ports.static_tryon import StaticTryOnRequest


@dataclass(frozen=True)
class RenderQualityObservation:
    """What a checker measured about one rendered image. All fields optional.

    `None` means "this checker did not determine it", which the rules layer
    grades differently from a failing value — see
    `visual_quality_rules.evaluate_render`.
    """

    #: Did the bytes decode as a valid image.
    decode_ok: bool | None = None
    #: Decoded pixel dimensions of the rendered file.
    pixel_width: int | None = None
    pixel_height: int | None = None
    #: Normalized luminance spread in [0, 1]. Near zero means a flat, blank or
    #: single-color frame.
    tonal_range: float | None = None
    #: Short identifier of what produced this observation, recorded in the
    #: persisted signals so a later reader knows which method judged the render.
    checker: str = "unknown"


class VisualQualityGatePort(Protocol):
    """Measure one rendered preview. Implementations must not infer anything
    about the person: no age, no gender, no identity, no body dimension, no
    aesthetic judgement — the `PersonFrameDetectorPort` rule, unchanged."""

    async def observe(
        self, rendered_image: bytes, request: StaticTryOnRequest
    ) -> RenderQualityObservation: ...
