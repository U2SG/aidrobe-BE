"""V3-018 — the quality fixture matrix, executed without a container.

`visual_quality_rules` is pure (no DB, no IO, no SQLAlchemy import), so every
verdict the gate can produce is pinned here as a function of a
`RenderQualityObservation` — the V3-005/006/007/012 pattern of proving the
judgement layer locally and leaving only the plumbing to the services tests.

Three claims, each with its counterexample in the same file:

1. **The matrix is total and fail-closed.** Every combination of measured /
   failing / missing values maps to exactly `show` or `hide`; a *required*
   check the gate could not run hides (unverified ≠ verified-with-known-gaps),
   while the four deferred §14 dimensions are reported `undetermined` and never
   block — the P0 scope line the feature's human gate exists to review.
2. **Thresholds are boundaries, not vibes.** The minimum resolution and tonal
   floor pass at exactly the constant and fail one unit below it, so a "tuned"
   constant fails a test instead of silently moving the gate.
3. **Acceptance 4 is structural.** `validate_render_signals` rejects keys off
   the allowlist, person-judgement terms (V3-012's deny-list, imported not
   copied), URL/base64 markers and any string long enough to smuggle image
   data — and a companion test proves the deny-list can still fail, because a
   filter nothing can trip is indistinguishable from no filter.
"""

from __future__ import annotations

from typing import Any

import pytest

from app.application.avatar.candidate_rules import ForbiddenQualitySignalError
from app.application.ports.visual_quality_gate import RenderQualityObservation
from app.application.purchase.visual_quality_rules import (
    ALLOWED_RENDER_SIGNAL_KEYS,
    CHECK_RENDER_CONTENT,
    CHECK_RENDER_INTEGRITY,
    CHECK_RENDER_RESOLUTION,
    DEFERRED_CHECKS,
    FAILED,
    MAX_SIGNAL_STRING_CHARS,
    MIN_RENDER_HEIGHT_PX,
    MIN_RENDER_WIDTH_PX,
    MIN_TONAL_RANGE,
    PASSED,
    RENDER_SIGNALS_VERSION,
    REQUIRED_CHECKS,
    UNDETERMINED,
    RenderVerdict,
    evaluate_render,
    finalize_render_signals,
    validate_render_signals,
)


def _observation(**overrides: Any) -> RenderQualityObservation:
    """A render that should be shown; each case breaks exactly one thing."""
    defaults: dict[str, Any] = {
        "decode_ok": True,
        "pixel_width": 768,
        "pixel_height": 1024,
        "tonal_range": 0.4,
        "checker": "matrix-fixture",
    }
    defaults.update(overrides)
    return RenderQualityObservation(**defaults)


# --- 1. the matrix -----------------------------------------------------------


@pytest.mark.parametrize(
    ("observation", "verdict", "failed", "undetermined_required"),
    [
        # A healthy render shows; nothing failed, nothing required is open.
        pytest.param(_observation(), RenderVerdict.SHOW, (), (), id="healthy"),
        # Bytes that are not an image fail every required check at once —
        # there is nothing to measure a resolution or a tonal range *of*.
        pytest.param(
            _observation(decode_ok=False, pixel_width=None, pixel_height=None, tonal_range=None),
            RenderVerdict.HIDE,
            (CHECK_RENDER_INTEGRITY, CHECK_RENDER_RESOLUTION, CHECK_RENDER_CONTENT),
            (),
            id="undecodable",
        ),
        # A thumbnail cannot carry a readable garment whatever else is true.
        pytest.param(
            _observation(pixel_width=200, pixel_height=300),
            RenderVerdict.HIDE,
            (CHECK_RENDER_RESOLUTION,),
            (),
            id="thumbnail",
        ),
        # One dimension under the floor is enough — width…
        pytest.param(
            _observation(pixel_width=MIN_RENDER_WIDTH_PX - 1),
            RenderVerdict.HIDE,
            (CHECK_RENDER_RESOLUTION,),
            (),
            id="width-one-px-short",
        ),
        # …and height, separately, so an `and` typoed to `or` fails here.
        pytest.param(
            _observation(pixel_height=MIN_RENDER_HEIGHT_PX - 1),
            RenderVerdict.HIDE,
            (CHECK_RENDER_RESOLUTION,),
            (),
            id="height-one-px-short",
        ),
        # A flat single-color frame — the classic silent provider failure.
        pytest.param(
            _observation(tonal_range=0.0),
            RenderVerdict.HIDE,
            (CHECK_RENDER_CONTENT,),
            (),
            id="blank-frame",
        ),
        # The boundaries belong to `show`: exactly the constants pass.
        pytest.param(
            _observation(
                pixel_width=MIN_RENDER_WIDTH_PX,
                pixel_height=MIN_RENDER_HEIGHT_PX,
                tonal_range=MIN_TONAL_RANGE,
            ),
            RenderVerdict.SHOW,
            (),
            (),
            id="exactly-at-every-floor",
        ),
        pytest.param(
            _observation(tonal_range=MIN_TONAL_RANGE - 0.001),
            RenderVerdict.HIDE,
            (CHECK_RENDER_CONTENT,),
            (),
            id="tonal-just-under-the-floor",
        ),
        # Fail closed: a required check the gate could not run is not a pass.
        pytest.param(
            _observation(decode_ok=None),
            RenderVerdict.HIDE,
            (),
            (CHECK_RENDER_INTEGRITY,),
            id="integrity-unmeasured",
        ),
        pytest.param(
            _observation(pixel_width=None, pixel_height=None),
            RenderVerdict.HIDE,
            (),
            (CHECK_RENDER_RESOLUTION,),
            id="resolution-unmeasured",
        ),
        pytest.param(
            _observation(tonal_range=None),
            RenderVerdict.HIDE,
            (),
            (CHECK_RENDER_CONTENT,),
            id="content-unmeasured",
        ),
    ],
)
def test_the_quality_fixture_matrix(
    observation: RenderQualityObservation,
    verdict: RenderVerdict,
    failed: tuple[str, ...],
    undetermined_required: tuple[str, ...],
) -> None:
    decision = evaluate_render(observation)

    assert decision.verdict is verdict
    assert tuple(decision.signals["failed_checks"]) == failed
    # The deferred §14 dimensions are ALWAYS open in P0; anything else open
    # must be exactly the required checks this case left unmeasured.
    assert set(decision.signals["undetermined_checks"]) == set(DEFERRED_CHECKS) | set(
        undetermined_required
    )


def test_a_missing_observation_hides_with_everything_undetermined() -> None:
    """A checker that measured nothing verified nothing — HIDE, not a guess.

    Deliberately the opposite polarity from V3-012's `assess_person_frame`
    (missing → `pending`): a render is at its only decision point, so there is
    no "later" for an unexamined image to wait for.
    """
    decision = evaluate_render(None)

    assert decision.verdict is RenderVerdict.HIDE
    assert decision.signals["checker"] == "unavailable"
    checks = decision.signals["checks"]
    assert set(checks) == set(REQUIRED_CHECKS) | set(DEFERRED_CHECKS)
    assert set(checks.values()) == {UNDETERMINED}


def test_the_verdict_vocabulary_is_two_values() -> None:
    """Show or hide; a third value would be a score nobody may act on
    differently (V3-005 acceptance 3's anti-goal, applied to the gate)."""
    assert {v.value for v in RenderVerdict} == {"show", "hide"}


def test_deferred_checks_never_block_and_are_always_reported() -> None:
    """The P0 scope line, both halves.

    A healthy render shows even though four §14 dimensions are open — and those
    four are *persisted* as `undetermined` so the human-gated benchmark review
    sees the gap instead of rediscovering it.
    """
    decision = evaluate_render(_observation())

    assert decision.verdict is RenderVerdict.SHOW
    for check in DEFERRED_CHECKS:
        assert decision.signals["checks"][check] == UNDETERMINED
    assert set(decision.signals["undetermined_checks"]) == set(DEFERRED_CHECKS)
    for check in REQUIRED_CHECKS:
        assert decision.signals["checks"][check] == PASSED


def test_every_grade_is_one_of_the_three_words() -> None:
    """The persisted vocabulary cannot grow a fourth grade unnoticed."""
    for observation in (None, _observation(), _observation(decode_ok=False)):
        decision = evaluate_render(observation)
        assert set(decision.signals["checks"].values()) <= {PASSED, FAILED, UNDETERMINED}


def test_measurements_are_rounded_not_precise() -> None:
    """Three decimals — enough to explain a verdict, not enough to invite
    anyone to read a stored measure as precise (the V3-012 convention)."""
    decision = evaluate_render(_observation(tonal_range=0.123456789))

    assert decision.signals["tonal_range"] == 0.123


def test_unmeasured_values_are_absent_not_null() -> None:
    """A key that is sometimes null invites `or 0` defaults downstream; a key
    that is absent reads as what it is — not measured."""
    decision = evaluate_render(_observation(tonal_range=None))

    assert "tonal_range" not in decision.signals
    assert decision.signals["pixel_width"] == 768


# --- 3. acceptance 4, structurally -------------------------------------------


def _healthy_signals() -> dict[str, Any]:
    return dict(evaluate_render(_observation()).signals)


def test_the_healthy_payload_passes_validation() -> None:
    """The companion control: the deny-list tests below prove rejection, this
    proves the filter is not rejecting everything."""
    signals = _healthy_signals()

    assert validate_render_signals(signals) is signals
    assert signals["signals_version"] == RENDER_SIGNALS_VERSION
    assert set(signals) <= ALLOWED_RENDER_SIGNAL_KEYS


@pytest.mark.parametrize(
    "mutation",
    [
        # A key nobody allowlisted — the growth path the allowlist closes.
        pytest.param({"minimax_task_id": "abc"}, id="off-allowlist-key"),
        # V3-012's person-judgement terms, inherited not copied: a nested
        # string value trips it, not just a top-level key.
        pytest.param({"checks": {"render_content": "body_ok"}}, id="person-term-in-value"),
        pytest.param({"checker": "skin-tone-check-2"}, id="person-term-in-checker"),
        # Image data and reachable resources, by marker…
        pytest.param({"checker": "http-fetcher"}, id="url-marker"),
        pytest.param({"failed_checks": ["data:image/png;base64,AAAA"]}, id="base64-marker"),
        pytest.param({"checker": "sig: signature=abc123"}, id="signature-marker"),
        # …and by size: nothing on the allowlist needs a long string, so an
        # embedded blob is structurally impossible, not merely unfashionable.
        pytest.param({"checker": "x" * (MAX_SIGNAL_STRING_CHARS + 1)}, id="oversized-string"),
    ],
)
def test_validation_rejects_what_acceptance_4_bans(mutation: dict[str, Any]) -> None:
    signals = _healthy_signals()
    signals.update(mutation)

    with pytest.raises(ForbiddenQualitySignalError):
        validate_render_signals(signals)


def test_finalize_adds_the_regeneration_accounting_and_revalidates() -> None:
    """The persisted payload is decision + accounting, validated as a whole —
    an accounting key bolted on at the call site would be exactly the
    unvalidated growth path the allowlist exists to close."""
    finalized = finalize_render_signals(_healthy_signals(), render_count=2, regen_budget=1)

    assert finalized["render_count"] == 2
    assert finalized["regen_budget"] == 1

    poisoned = _healthy_signals()
    poisoned["object_key_hint"] = "users/x/tryon.png"
    with pytest.raises(ForbiddenQualitySignalError):
        finalize_render_signals(poisoned, render_count=1, regen_budget=1)


def test_the_check_names_avoid_the_person_judgement_vocabulary() -> None:
    """§14 says "Identity/Body Fidelity"; the persisted names say `likeness` /
    `proportion` because `identity` and `body` are deny-listed terms. This test
    is what keeps a rename from quietly reintroducing them: every name the
    module can persist must itself survive validation."""
    everything = evaluate_render(_observation(decode_ok=False))

    # If any check name, grade or version string contained a banned term, the
    # decision above would have raised inside `_decision` already; assert it
    # again explicitly so the guarantee is stated rather than incidental.
    assert validate_render_signals(dict(everything.signals))
