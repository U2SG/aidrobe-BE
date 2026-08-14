"""V3-012 — grade one photo as avatar candidate material. Pure.

No DB, no IO, no SQLAlchemy import — the V3-004 `candidate_normalizer` /
V3-005 `wardrobe_similarity` / V3-006 `outfit_composition` split. Everything
here is a function of a `PersonFrameObservation` and the constants below, so
the fixture matrix in tests/unit runs without a container.

**Three answers, no score.** `AvatarQualityStatus` is `passed` / `rejected` /
`pending` and V3-011 froze it that way so "a quality signal can never become a
judgement about the person". `pending` is load-bearing: it means *we did not
determine this*, which is a different statement from `rejected` (*we looked and
it is not usable*). Collapsing the two would let an undetected photo read as a
refused one, and V3_SPEC §7.3's prompt-once rule would then permanently retire
material we never actually examined.

**What the checks are, and what they honestly prove** (V3_SPEC §7.4 minimums):

| §7.4 requirement | check | determined here? |
|---|---|---|
| 单人 | `single_subject` | yes, as "exactly one disjoint foreground region" |
| 身体大部分完整 / 无严重遮挡 | `subject_framing` | partially — framing only |
| 清晰度足够 | `sharpness` | yes |
| 正面或轻微侧面 | `orientation` | **no** — not derivable from mask geometry |
| 图像内容符合安全要求 | `content_safety` | **no** — needs a content classifier |

The last two are reported as `undetermined` rather than silently omitted, and
they do **not** block `passed`. That is a deliberate scope line, not an
oversight: V3-012 detects reusable material, V3-013's preparation and V3-018's
visual quality gate own what has to be true before anything is generated from
it. Recording them keeps the gap visible to those features instead of leaving
them to rediscover it.

**Acceptance 3 is structural here.** `ALLOWED_QUALITY_SIGNAL_KEYS` is an
allowlist of every key that may reach the persisted JSONB, and
`FORBIDDEN_SIGNAL_TERMS` is a deny-list of the concepts that must never appear
in a key or a string value. `validate_quality_signals` enforces both and is
called on the way out of `assess_person_frame`, so a signal vocabulary that
grew an age band or a body measurement would fail in this module rather than in
review. The allowlist is the stronger half; the deny-list is what makes the
intent legible to the next person who wants to add a field.
"""

from dataclasses import dataclass
from typing import Any

from app.application.ports.avatar_detection import PersonFrameObservation
from app.common.enums import AvatarQualityStatus

#: Bumped when the meaning of a signal changes, so a stored row can always be
#: read back against the rules that produced it (the V3-005/006
#: `evidence_version` convention).
QUALITY_SIGNALS_VERSION = "v3-012.1"

# --- thresholds ------------------------------------------------------------
# Judgement calls, not measured constants. They are named and gathered here so
# a future tuning round is a one-line diff with a test, rather than a hunt
# through branches.

#: 单人. More than one disjoint foreground region means more than one subject
#: (or a subject plus a large object); zero means nothing was found.
EXPECTED_SUBJECT_REGION_COUNT = 1
#: A figure standing in a frame occupies a modest share of it. Below the floor
#: the subject is a speck; above the ceiling the frame is filled, which is what
#: a close-up crop looks like — and a close-up is not reusable as a whole-figure
#: reference.
MIN_SUBJECT_FRAME_FILL_RATIO = 0.05
MAX_SUBJECT_FRAME_FILL_RATIO = 0.80
#: The subject's bounding box must span most of the frame's height. Combined
#: with the fill ceiling this is the discriminator between a whole figure
#: (tall box, moderate fill) and a head-and-shoulders crop (tall box, high fill).
MIN_SUBJECT_VERTICAL_SPAN_RATIO = 0.55
#: Foreground running along the top edge means the picture is cut above the
#: subject — the "身体大部分完整" failure that matters most, because a missing
#: head cannot be recovered by any later step.
MAX_TOP_EDGE_CONTACT_RATIO = 0.15
#: 清晰度足够.
MIN_SHARPNESS_SCORE = 0.15
#: Below this a photo cannot carry usable detail whatever else is true.
MIN_PIXEL_WIDTH = 320
MIN_PIXEL_HEIGHT = 320

CHECK_SINGLE_SUBJECT = "single_subject"
CHECK_SUBJECT_FRAMING = "subject_framing"
CHECK_SHARPNESS = "sharpness"
CHECK_RESOLUTION = "resolution"
CHECK_ORIENTATION = "orientation"
CHECK_CONTENT_SAFETY = "content_safety"

#: Checks that must all be `passed` for material to be offered as a candidate.
REQUIRED_CHECKS: tuple[str, ...] = (
    CHECK_SINGLE_SUBJECT,
    CHECK_SUBJECT_FRAMING,
    CHECK_SHARPNESS,
    CHECK_RESOLUTION,
)
#: Checks this feature reports but cannot determine. See the module docstring.
DEFERRED_CHECKS: tuple[str, ...] = (CHECK_ORIENTATION, CHECK_CONTENT_SAFETY)

PASSED = "passed"
FAILED = "failed"
UNDETERMINED = "undetermined"

#: Every key that may appear in the persisted `quality_signals` JSONB.
ALLOWED_QUALITY_SIGNAL_KEYS: frozenset[str] = frozenset(
    {
        "signals_version",
        "detector",
        "checks",
        "failed_checks",
        "undetermined_checks",
        "subject_region_count",
        "subject_frame_fill_ratio",
        "subject_vertical_span_ratio",
        "top_edge_contact_ratio",
        "sharpness_score",
        "frame_width_px",
        "frame_height_px",
    }
)

#: Concepts that must never appear in a signal key or a signal string value.
#: This is the readable statement of acceptance 3 and of contract §7's "no
#: body-shape commentary of any kind (no slimming, age, attractiveness or 'fix'
#: language)". Substring matching is deliberate — `age_band`, `bodyshape` and
#: `estimated_age` all have to fail, not just exact names, which is also why
#: the framing measurement is named `subject_frame_fill_ratio`: the natural
#: word "coverage" contains "age" and would have needed an exception, and an
#: exception list is how a deny-list stops meaning anything.
#:
#: Two words are deliberately absent. `height` / `width` describe the image
#: file (`frame_height_px`) and banning them would ban that; `score` is absent
#: because `sharpness_score` scores the photograph's focus, not the person.
#: Both cases are covered by `ALLOWED_QUALITY_SIGNAL_KEYS` instead — an
#: `estimated_height_cm` signal fails the allowlist even though the deny-list
#: would let it through. The allowlist is the guarantee; this list is the
#: explanation.
FORBIDDEN_SIGNAL_TERMS: frozenset[str] = frozenset(
    {
        "age",
        "attractive",
        "attractiveness",
        "beauty",
        "beautiful",
        "bmi",
        "body",
        "bust",
        "chest",
        "curve",
        "ethnic",
        "fat",
        "figure",
        "gender",
        "handsome",
        "hip",
        "identity",
        "muscle",
        "obese",
        "physique",
        "pretty",
        "race",
        "sex",
        "shape",
        "skin",
        "slim",
        "thin",
        "ugly",
        "waist",
        "weight",
    }
)


class ForbiddenQualitySignalError(ValueError):
    """A signal payload named or described something about the person."""


@dataclass(frozen=True)
class CandidateAssessment:
    """The verdict on one photo, plus the neutral signals behind it."""

    quality_status: AvatarQualityStatus
    signals: dict[str, Any]

    @property
    def is_offerable(self) -> bool:
        """True only when the material may be surfaced as a candidate."""
        return self.quality_status is AvatarQualityStatus.PASSED


def validate_quality_signals(signals: dict[str, Any]) -> dict[str, Any]:
    """Return `signals` unchanged, or raise if it breaks acceptance 3.

    Checked on every path out of this module rather than at the persistence
    boundary: the point is that a forbidden signal cannot be *constructed*,
    not merely that it cannot be saved.
    """
    for key in signals:
        if key not in ALLOWED_QUALITY_SIGNAL_KEYS:
            raise ForbiddenQualitySignalError(f"quality signal key is not on the allowlist: {key!r}")
    for term in _all_words(signals):
        for forbidden in FORBIDDEN_SIGNAL_TERMS:
            if forbidden in term:
                raise ForbiddenQualitySignalError(
                    f"quality signals must carry no judgement about the person: {term!r}"
                )
    return signals


def _all_words(value: Any) -> list[str]:
    """Every key and string value in a nested payload, lowercased."""
    words: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            words.append(str(key).lower())
            words.extend(_all_words(item))
    elif isinstance(value, list | tuple):
        for item in value:
            words.extend(_all_words(item))
    elif isinstance(value, str):
        words.append(value.lower())
    return words


def assess_person_frame(observation: PersonFrameObservation | None) -> CandidateAssessment:
    """Grade one photo. Never raises on a partial or absent observation.

    A missing observation is `pending` with every check undetermined — the
    honest answer when the detector could not run at all (no optional
    dependency installed, unreadable bytes, storage miss). It is emphatically
    not `rejected`: nothing was examined.
    """
    if observation is None:
        return _assessment(
            {check: UNDETERMINED for check in REQUIRED_CHECKS + DEFERRED_CHECKS},
            detector="unavailable",
            measurements={},
        )

    checks = {
        CHECK_SINGLE_SUBJECT: _grade_single_subject(observation),
        CHECK_SUBJECT_FRAMING: _grade_subject_framing(observation),
        CHECK_SHARPNESS: _grade_sharpness(observation),
        CHECK_RESOLUTION: _grade_resolution(observation),
    }
    # Reported, never determined here. See the module docstring's table.
    checks.update({check: UNDETERMINED for check in DEFERRED_CHECKS})

    measurements = {
        "subject_region_count": observation.subject_region_count,
        "subject_frame_fill_ratio": _round(observation.subject_frame_fill_ratio),
        "subject_vertical_span_ratio": _round(observation.subject_vertical_span_ratio),
        "top_edge_contact_ratio": _round(observation.top_edge_contact_ratio),
        "sharpness_score": _round(observation.sharpness_score),
        "frame_width_px": observation.pixel_width,
        "frame_height_px": observation.pixel_height,
    }
    return _assessment(checks, detector=observation.detector, measurements=measurements)


def _assessment(
    checks: dict[str, str],
    *,
    detector: str,
    measurements: dict[str, Any],
) -> CandidateAssessment:
    failed = tuple(name for name in REQUIRED_CHECKS if checks.get(name) == FAILED)
    undetermined = tuple(name for name, value in sorted(checks.items()) if value == UNDETERMINED)
    if failed:
        status = AvatarQualityStatus.REJECTED
    elif any(checks.get(name) == UNDETERMINED for name in REQUIRED_CHECKS):
        # A required check we could not run. Not usable, and not refused
        # either — the photo can be re-examined once a detector is available.
        status = AvatarQualityStatus.PENDING
    else:
        status = AvatarQualityStatus.PASSED

    signals: dict[str, Any] = {
        "signals_version": QUALITY_SIGNALS_VERSION,
        "detector": detector,
        "checks": dict(sorted(checks.items())),
        "failed_checks": list(failed),
        "undetermined_checks": list(undetermined),
    }
    signals.update({key: value for key, value in measurements.items() if value is not None})
    return CandidateAssessment(quality_status=status, signals=validate_quality_signals(signals))


def _grade_single_subject(observation: PersonFrameObservation) -> str:
    if observation.subject_region_count is None:
        return UNDETERMINED
    return PASSED if observation.subject_region_count == EXPECTED_SUBJECT_REGION_COUNT else FAILED


def _grade_subject_framing(observation: PersonFrameObservation) -> str:
    coverage = observation.subject_frame_fill_ratio
    span = observation.subject_vertical_span_ratio
    top_contact = observation.top_edge_contact_ratio
    if coverage is None or span is None or top_contact is None:
        return UNDETERMINED
    if not MIN_SUBJECT_FRAME_FILL_RATIO <= coverage <= MAX_SUBJECT_FRAME_FILL_RATIO:
        return FAILED
    if span < MIN_SUBJECT_VERTICAL_SPAN_RATIO:
        return FAILED
    if top_contact > MAX_TOP_EDGE_CONTACT_RATIO:
        return FAILED
    return PASSED


def _grade_sharpness(observation: PersonFrameObservation) -> str:
    if observation.sharpness_score is None:
        return UNDETERMINED
    return PASSED if observation.sharpness_score >= MIN_SHARPNESS_SCORE else FAILED


def _grade_resolution(observation: PersonFrameObservation) -> str:
    if observation.pixel_width is None or observation.pixel_height is None:
        return UNDETERMINED
    fits = observation.pixel_width >= MIN_PIXEL_WIDTH and observation.pixel_height >= MIN_PIXEL_HEIGHT
    return PASSED if fits else FAILED


def _round(value: float | None) -> float | None:
    """Three decimals — enough to explain a verdict, not enough to invite
    anyone to read a stored ratio as a precise measurement of anything."""
    return None if value is None else round(float(value), 3)
