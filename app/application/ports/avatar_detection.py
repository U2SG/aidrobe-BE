"""V3-012 — the port a person-frame detector implements.

`PersonFrameObservation` is deliberately a set of **measurements of the
photograph**, never inferences about the person in it. That distinction is
V3-012 acceptance 3 ("quality signals avoid age, attractiveness or body
scoring") expressed as a type: there is no field a body inference could be
written into, so the rule layer downstream cannot accidentally consume one.

Read the field names literally:

- `subject_region_count` counts disjoint regions in a foreground mask. It says
  "there is more than one separated shape here", which is the honest proxy for
  V3_SPEC §7.4's 单人 requirement. It is not a person classifier.
- `subject_frame_fill_ratio` / `subject_vertical_span_ratio` /
  `top_edge_contact_ratio` describe **framing** — how the subject sits inside
  the frame. They depend on camera distance and crop, so they carry no
  information about the person's size or shape; a tall figure photographed far
  away and a short figure photographed close produce the same numbers.
- `sharpness_score` is a focus measure of the image, i.e. §7.4's 清晰度足够.

Two of §7.4's minimums have **no field here on purpose**: 正面或轻微侧面
(orientation) and 图像内容符合安全要求 (content safety). Neither is derivable
from mask geometry, and inventing a field for them would invite a detector to
claim a determination it never made. They stay `undetermined` all the way
through to the persisted signals, where V3-013's preparation and V3-018's
visual quality gate can see they are still open.

Every field is optional. `None` means "this detector did not determine it",
which is a different answer from a failing value and is graded differently by
`app.application.avatar.candidate_rules`.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class PersonFrameObservation:
    """What a detector measured about one photo. All fields optional."""

    #: Number of disjoint foreground regions large enough to be a subject.
    subject_region_count: int | None = None
    #: Foreground pixels / frame pixels, in [0, 1].
    subject_frame_fill_ratio: float | None = None
    #: Height of the foreground bounding box / frame height, in [0, 1].
    subject_vertical_span_ratio: float | None = None
    #: Fraction of the frame's top edge the foreground touches, in [0, 1]. A
    #: high value means the subject runs off the top of the picture.
    top_edge_contact_ratio: float | None = None
    #: Normalized focus measure in [0, 1]. Higher is sharper.
    sharpness_score: float | None = None
    #: Decoded pixel dimensions of the image file itself.
    pixel_width: int | None = None
    pixel_height: int | None = None
    #: Short identifier of what produced this observation, recorded in the
    #: persisted signals so a later reader knows which method judged the photo.
    detector: str = "unknown"


class PersonFrameDetectorPort(Protocol):
    """Measure one image. Implementations must not infer anything about the
    person: no age, no gender, no identity, no body dimension, no aesthetic
    judgement. Raising is a legitimate answer — callers treat any failure as
    "not determined" and leave the photo unassessed."""

    async def observe(self, image_bytes: bytes) -> PersonFrameObservation: ...
