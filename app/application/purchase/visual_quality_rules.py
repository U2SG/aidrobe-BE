"""V3-018 — decide whether one rendered preview may be shown. Pure.

No DB, no IO, no SQLAlchemy import — the V3-004/005/006/012 split, so the
quality fixture matrix in tests/unit runs without a container. Everything here
is a function of a `RenderQualityObservation` and the constants below.

**Two answers, no score.** The verdict is `show` or `hide` and nothing in
between reaches a caller. Contract §6.3 collapses V3_SPEC §14's display policy
into one frozen rule — "at most one model retry after a quality-gate failure,
then `rejected_by_quality_gate`" — so a graded confidence would be a number
nobody may act on differently, and V3-005 acceptance 3 already established that
an unactionable score is a liability, not information. A regeneration is the
*worker's* move (`static_tryon_worker`), budgeted by
`Settings.TRYON_QUALITY_MAX_AUTO_REGENERATIONS`; this module only ever judges
the render in front of it.

**What the checks are, and what they honestly prove** (V3_SPEC §14):

| §14 dimension | check | determined in P0? |
|---|---|---|
| Artifact — blank/garbage output | `render_integrity`, `render_content` | yes, as decode + tonal-spread |
| Artifact — degraded output size | `render_resolution` | yes |
| Identity fidelity — 人脸是否稳定 | `likeness_fidelity` | **no** — needs a comparison model |
| Body fidelity — 比例是否被改变 | `proportion_fidelity` | **no** — same |
| Garment fidelity — 颜色/图案/廓形 | `garment_fidelity` | **no** — same |
| Artifact — 手指/多余肢体/Logo 扭曲 | `artifact_scan` | **no** — same |

The four deferred checks are reported `undetermined` rather than silently
omitted, and they do **not** block `show`. That is the P0 scope line this
feature's human gate exists for: "final visual quality threshold requires
benchmark review before production" — the benchmark review is what turns those
dimensions into determinations, and the persisted signals keep the gap visible
to it instead of leaving it to be rediscovered. A *required* check that comes
back undetermined is a different situation entirely: the gate malfunctioned,
and the render is unverified rather than verified-with-known-gaps — so it
hides. Fail closed, because "return every successful provider response
unchecked" is exactly what this feature exists to stop.

**The deferred names avoid the deny-list on purpose.** §14 calls the first two
dimensions "Identity Fidelity" and "Body Fidelity", but `identity` and `body`
are on V3-012's `FORBIDDEN_SIGNAL_TERMS` — which this module imports wholesale
rather than maintaining a second, weaker copy. So the persisted vocabulary says
`likeness_fidelity` (does the render still look like the source) and
`proportion_fidelity` (did the render change the source's proportions): the
same claims, stated about the *render's faithfulness to its input* in words
that can never be confused with a judgement about the person. Same move as
V3-012 naming its framing measure `subject_frame_fill_ratio` because
"coverage" contains "age".

**Acceptance 4 is structural here, twice over.** `ALLOWED_RENDER_SIGNAL_KEYS`
is an allowlist of every key that may reach the persisted JSONB or a log line;
`validate_render_signals` also inherits V3-012's person-judgement deny-list and
adds this module's own: no string may exceed `MAX_SIGNAL_STRING_CHARS`, and
none may contain a URL or base64 marker — which is what "quality logs contain
no sensitive image data" means mechanically. A signal that grew an image blob,
a signed URL or an age band fails in this module, not in review.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from app.application.avatar.candidate_rules import (
    FORBIDDEN_SIGNAL_TERMS,
    ForbiddenQualitySignalError,
)
from app.application.ports.visual_quality_gate import RenderQualityObservation

#: Bumped when the meaning of a signal changes, so a stored row can always be
#: read back against the rules that produced it (the V3-005/006/012
#: `evidence_version` convention).
RENDER_SIGNALS_VERSION = "v3-018.1"

# --- thresholds --------------------------------------------------------------
# Judgement calls, not measured constants — named and gathered here so the
# human-gated benchmark review is a one-line diff with a test, rather than a
# hunt through branches. The V3-012 convention.

#: Below this a render cannot carry a readable garment whatever else is true.
#: Half the fake pipeline's 768×1024 canvas, floor-ish; a real provider that
#: returns thumbnails has failed the user even if the pixels are pretty.
MIN_RENDER_WIDTH_PX = 384
MIN_RENDER_HEIGHT_PX = 512
#: A flat single-color frame — the classic silent provider failure — measures
#: ~0.0; any render that actually contains a figure and garments measures far
#: above this. The floor only has to catch "there is nothing in this picture".
MIN_TONAL_RANGE = 0.02

CHECK_RENDER_INTEGRITY = "render_integrity"
CHECK_RENDER_RESOLUTION = "render_resolution"
CHECK_RENDER_CONTENT = "render_content"
CHECK_LIKENESS_FIDELITY = "likeness_fidelity"
CHECK_PROPORTION_FIDELITY = "proportion_fidelity"
CHECK_GARMENT_FIDELITY = "garment_fidelity"
CHECK_ARTIFACT_SCAN = "artifact_scan"

#: Checks that must all be `passed` for a render to be shown.
REQUIRED_CHECKS: tuple[str, ...] = (
    CHECK_RENDER_INTEGRITY,
    CHECK_RENDER_RESOLUTION,
    CHECK_RENDER_CONTENT,
)
#: §14 dimensions P0 cannot determine. See the module docstring's table.
DEFERRED_CHECKS: tuple[str, ...] = (
    CHECK_LIKENESS_FIDELITY,
    CHECK_PROPORTION_FIDELITY,
    CHECK_GARMENT_FIDELITY,
    CHECK_ARTIFACT_SCAN,
)

PASSED = "passed"
FAILED = "failed"
UNDETERMINED = "undetermined"

#: Every key that may appear in the persisted `quality_signals` JSONB or in a
#: quality log line.
ALLOWED_RENDER_SIGNAL_KEYS: frozenset[str] = frozenset(
    {
        "signals_version",
        "checker",
        "verdict",
        "checks",
        "failed_checks",
        "undetermined_checks",
        "render_count",
        "regen_budget",
        "pixel_width",
        "pixel_height",
        "tonal_range",
    }
)

#: Markers of image data or reachable resources — the concepts acceptance 4
#: ("quality logs contain no sensitive image data") bans by name. V3-012's
#: person-judgement deny-list is imported and enforced alongside; this list is
#: the render-specific half.
FORBIDDEN_IMAGE_DATA_TERMS: frozenset[str] = frozenset(
    {
        "http",
        "://",
        "base64",
        "signature",
        "object_key",
        "image_url",
    }
)

#: Nothing on the allowlist needs a long string — check names, grades and a
#: checker slug are all short. Bounding every string is what makes an embedded
#: base64 blob or data URI structurally impossible rather than merely
#: unfashionable.
MAX_SIGNAL_STRING_CHARS = 64


class RenderVerdict(StrEnum):
    """May this render be shown. Two values; there is no third."""

    SHOW = "show"
    HIDE = "hide"


@dataclass(frozen=True)
class GateDecision:
    """The verdict on one render, plus the neutral signals behind it."""

    verdict: RenderVerdict
    signals: dict[str, Any]


def validate_render_signals(signals: dict[str, Any]) -> dict[str, Any]:
    """Return `signals` unchanged, or raise if it breaks acceptance 4.

    Checked on every path out of this module rather than at the persistence
    boundary: the point is that a forbidden signal cannot be *constructed*,
    not merely that it cannot be saved.
    """
    for key in signals:
        if key not in ALLOWED_RENDER_SIGNAL_KEYS:
            raise ForbiddenQualitySignalError(
                f"render signal key is not on the allowlist: {key!r}"
            )
    for term in _all_strings(signals):
        if len(term) > MAX_SIGNAL_STRING_CHARS:
            raise ForbiddenQualitySignalError(
                "render signals must not carry long strings (image data cannot fit)"
            )
        lowered = term.lower()
        for forbidden in FORBIDDEN_SIGNAL_TERMS | FORBIDDEN_IMAGE_DATA_TERMS:
            if forbidden in lowered:
                raise ForbiddenQualitySignalError(
                    f"render signals must carry no judgement or image data: {term!r}"
                )
    return signals


def _all_strings(value: Any) -> list[str]:
    """Every key and string value in a nested payload."""
    strings: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            strings.append(str(key))
            strings.extend(_all_strings(item))
    elif isinstance(value, list | tuple):
        for item in value:
            strings.extend(_all_strings(item))
    elif isinstance(value, str):
        strings.append(value)
    return strings


def evaluate_render(observation: RenderQualityObservation | None) -> GateDecision:
    """Judge one render. Never raises on a partial or absent observation.

    A missing observation hides — the checker measured nothing, so the render
    is unverified, and an unverified render of a person's likeness does not
    ship. This is deliberately the opposite polarity from V3-012's
    `assess_person_frame`, whose missing observation is `pending`: an
    unexamined *photo* can wait to be examined later, but a render is at its
    only decision point right now — there is no later examination, only the
    §6.2 terminal states.
    """
    if observation is None:
        checks = {check: UNDETERMINED for check in REQUIRED_CHECKS + DEFERRED_CHECKS}
        return _decision(checks, checker="unavailable", measurements={})

    checks = {
        CHECK_RENDER_INTEGRITY: _grade_integrity(observation),
        CHECK_RENDER_RESOLUTION: _grade_resolution(observation),
        CHECK_RENDER_CONTENT: _grade_content(observation),
    }
    # Reported, never determined here. See the module docstring's table.
    checks.update({check: UNDETERMINED for check in DEFERRED_CHECKS})

    measurements = {
        "pixel_width": observation.pixel_width,
        "pixel_height": observation.pixel_height,
        "tonal_range": _round(observation.tonal_range),
    }
    return _decision(checks, checker=observation.checker, measurements=measurements)


def finalize_render_signals(
    signals: dict[str, Any], *, render_count: int, regen_budget: int
) -> dict[str, Any]:
    """The persisted payload: the decision's signals plus the regen accounting.

    Lives here rather than in the worker so the *complete* payload passes
    through `validate_render_signals` — an accounting key added at the call
    site would be exactly the unvalidated growth path the allowlist exists to
    close.
    """
    return validate_render_signals(
        {**signals, "render_count": render_count, "regen_budget": regen_budget}
    )


def _decision(
    checks: dict[str, str],
    *,
    checker: str,
    measurements: dict[str, Any],
) -> GateDecision:
    failed = tuple(name for name in REQUIRED_CHECKS if checks.get(name) == FAILED)
    undetermined = tuple(
        name for name, value in sorted(checks.items()) if value == UNDETERMINED
    )
    # Fail closed: a failed required check hides, and so does a required check
    # the gate could not run — see the module docstring on the asymmetry with
    # the deferred checks, which never block.
    unverified = any(checks.get(name) == UNDETERMINED for name in REQUIRED_CHECKS)
    verdict = RenderVerdict.HIDE if failed or unverified else RenderVerdict.SHOW

    signals: dict[str, Any] = {
        "signals_version": RENDER_SIGNALS_VERSION,
        "checker": checker,
        "verdict": verdict.value,
        "checks": dict(sorted(checks.items())),
        "failed_checks": list(failed),
        "undetermined_checks": list(undetermined),
    }
    signals.update({key: value for key, value in measurements.items() if value is not None})
    return GateDecision(verdict=verdict, signals=validate_render_signals(signals))


def _grade_integrity(observation: RenderQualityObservation) -> str:
    if observation.decode_ok is None:
        return UNDETERMINED
    return PASSED if observation.decode_ok else FAILED


def _grade_resolution(observation: RenderQualityObservation) -> str:
    if observation.decode_ok is False:
        # Nothing decoded, so there are no dimensions to grade — but that is
        # the integrity check's failure, not a second undetermined gap.
        return FAILED
    if observation.pixel_width is None or observation.pixel_height is None:
        return UNDETERMINED
    fits = (
        observation.pixel_width >= MIN_RENDER_WIDTH_PX
        and observation.pixel_height >= MIN_RENDER_HEIGHT_PX
    )
    return PASSED if fits else FAILED


def _grade_content(observation: RenderQualityObservation) -> str:
    if observation.decode_ok is False:
        return FAILED
    if observation.tonal_range is None:
        return UNDETERMINED
    return PASSED if observation.tonal_range >= MIN_TONAL_RANGE else FAILED


def _round(value: float | None) -> float | None:
    """Three decimals — enough to explain a verdict, not enough to invite
    anyone to read a stored measure as precise (the V3-012 convention)."""
    return None if value is None else round(float(value), 3)
