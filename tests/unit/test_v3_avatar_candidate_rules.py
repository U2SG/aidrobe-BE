"""V3-012 — the candidate fixture matrix and the sensitive-inference deny test.

Pure: `candidate_rules` imports no DB, no IO and no SQLAlchemy, so this file
runs under `--noconftest` with no container. That is deliberate — it is the one
piece of V3-012 evidence that is genuinely local on this host, not only CI.

Two of the three `evidence_required` items live here: the **fixture matrix**
(every §7.4 check, at its pass side, its fail side and its undetermined side)
and the **sensitive-inference deny test**.
"""

import pytest

from app.application.avatar.candidate_rules import (
    ALLOWED_QUALITY_SIGNAL_KEYS,
    CHECK_CONTENT_SAFETY,
    CHECK_ORIENTATION,
    CHECK_RESOLUTION,
    CHECK_SHARPNESS,
    CHECK_SINGLE_SUBJECT,
    CHECK_SUBJECT_FRAMING,
    DEFERRED_CHECKS,
    FAILED,
    FORBIDDEN_SIGNAL_TERMS,
    MAX_SUBJECT_FRAME_FILL_RATIO,
    MIN_PIXEL_HEIGHT,
    MIN_PIXEL_WIDTH,
    MIN_SHARPNESS_SCORE,
    MIN_SUBJECT_FRAME_FILL_RATIO,
    MIN_SUBJECT_VERTICAL_SPAN_RATIO,
    PASSED,
    QUALITY_SIGNALS_VERSION,
    REQUIRED_CHECKS,
    UNDETERMINED,
    ForbiddenQualitySignalError,
    assess_person_frame,
    validate_quality_signals,
)
from app.application.ports.avatar_detection import PersonFrameObservation
from app.common.enums import AvatarQualityStatus

# A photo that passes everything V3-012 can determine. Every fixture below is
# this, with exactly one measurement moved, so a verdict change is always
# attributable to one field.
GOOD = PersonFrameObservation(
    subject_region_count=1,
    subject_frame_fill_ratio=0.35,
    subject_vertical_span_ratio=0.9,
    top_edge_contact_ratio=0.0,
    sharpness_score=0.6,
    pixel_width=1080,
    pixel_height=1920,
    detector="test_detector",
)


def _with(**changes: object) -> PersonFrameObservation:
    from dataclasses import replace

    return replace(GOOD, **changes)  # type: ignore[arg-type]


# --- the offerable baseline ------------------------------------------------


def test_a_clean_full_body_photo_passes() -> None:
    assessment = assess_person_frame(GOOD)
    assert assessment.quality_status is AvatarQualityStatus.PASSED
    assert assessment.is_offerable is True
    assert assessment.signals["failed_checks"] == []
    assert assessment.signals["signals_version"] == QUALITY_SIGNALS_VERSION
    assert assessment.signals["detector"] == "test_detector"


def test_the_two_undeterminable_checks_are_reported_and_do_not_block_passing() -> None:
    """§7.4 lists orientation and content safety; mask geometry cannot answer
    either. They must be visible as open rather than silently dropped — and they
    must not veto material, or nothing would ever be offerable."""
    signals = assess_person_frame(GOOD).signals
    checks = signals["checks"]
    for deferred in DEFERRED_CHECKS:
        assert checks[deferred] == UNDETERMINED
    assert set(signals["undetermined_checks"]) == set(DEFERRED_CHECKS)
    assert CHECK_ORIENTATION in DEFERRED_CHECKS
    assert CHECK_CONTENT_SAFETY in DEFERRED_CHECKS
    # ...and the verdict is still PASSED, asserted above but restated as the
    # point of this test.
    assert assess_person_frame(GOOD).quality_status is AvatarQualityStatus.PASSED


# --- the fixture matrix: one row per §7.4 check, failing side -------------


@pytest.mark.parametrize(
    ("label", "observation", "expected_failed_check"),
    [
        ("two people", _with(subject_region_count=2), CHECK_SINGLE_SUBJECT),
        ("nobody found", _with(subject_region_count=0), CHECK_SINGLE_SUBJECT),
        (
            "subject is a speck",
            _with(subject_frame_fill_ratio=MIN_SUBJECT_FRAME_FILL_RATIO / 2),
            CHECK_SUBJECT_FRAMING,
        ),
        (
            "close-up crop fills the frame",
            _with(subject_frame_fill_ratio=MAX_SUBJECT_FRAME_FILL_RATIO + 0.05),
            CHECK_SUBJECT_FRAMING,
        ),
        (
            "only a short slice of the height",
            _with(subject_vertical_span_ratio=MIN_SUBJECT_VERTICAL_SPAN_RATIO - 0.05),
            CHECK_SUBJECT_FRAMING,
        ),
        (
            "cut off above the head",
            _with(top_edge_contact_ratio=0.6),
            CHECK_SUBJECT_FRAMING,
        ),
        ("out of focus", _with(sharpness_score=MIN_SHARPNESS_SCORE / 2), CHECK_SHARPNESS),
        (
            "too small to carry detail",
            _with(pixel_width=MIN_PIXEL_WIDTH - 1, pixel_height=MIN_PIXEL_HEIGHT - 1),
            CHECK_RESOLUTION,
        ),
    ],
)
def test_rejected_fixtures(
    label: str, observation: PersonFrameObservation, expected_failed_check: str
) -> None:
    assessment = assess_person_frame(observation)
    assert assessment.quality_status is AvatarQualityStatus.REJECTED, label
    assert assessment.is_offerable is False, label
    assert expected_failed_check in assessment.signals["failed_checks"], label
    assert assessment.signals["checks"][expected_failed_check] == FAILED, label


@pytest.mark.parametrize(
    ("label", "observation", "undetermined_check"),
    [
        ("no region count", _with(subject_region_count=None), CHECK_SINGLE_SUBJECT),
        ("no framing numbers", _with(subject_frame_fill_ratio=None), CHECK_SUBJECT_FRAMING),
        ("no span", _with(subject_vertical_span_ratio=None), CHECK_SUBJECT_FRAMING),
        ("no top-edge measure", _with(top_edge_contact_ratio=None), CHECK_SUBJECT_FRAMING),
        ("no focus measure", _with(sharpness_score=None), CHECK_SHARPNESS),
        ("no dimensions", _with(pixel_width=None), CHECK_RESOLUTION),
    ],
)
def test_an_undetermined_required_check_is_pending_not_rejected(
    label: str, observation: PersonFrameObservation, undetermined_check: str
) -> None:
    """The distinction the whole three-state vocabulary exists for.

    `pending` means "we did not determine this"; `rejected` means "we looked and
    it is not usable". Collapsing them would let an unexamined photo read as a
    refused one, and prompt-once would then permanently retire material nobody
    ever assessed.
    """
    assessment = assess_person_frame(observation)
    assert assessment.quality_status is AvatarQualityStatus.PENDING, label
    assert assessment.is_offerable is False, label
    assert assessment.signals["checks"][undetermined_check] == UNDETERMINED, label
    assert assessment.signals["failed_checks"] == [], label


def test_a_real_failure_outranks_a_missing_measurement() -> None:
    """Two people AND no focus measure is a rejection, not a pending. We know
    enough to refuse; the missing number cannot rescue it."""
    assessment = assess_person_frame(_with(subject_region_count=3, sharpness_score=None))
    assert assessment.quality_status is AvatarQualityStatus.REJECTED
    assert CHECK_SINGLE_SUBJECT in assessment.signals["failed_checks"]


def test_no_observation_at_all_is_pending_with_everything_undetermined() -> None:
    """The detector could not run — no optional dependency, unreadable bytes.
    Nothing was examined, so nothing may be refused."""
    assessment = assess_person_frame(None)
    assert assessment.quality_status is AvatarQualityStatus.PENDING
    assert assessment.signals["detector"] == "unavailable"
    checks = assessment.signals["checks"]
    assert set(checks) == set(REQUIRED_CHECKS + DEFERRED_CHECKS)
    assert all(value == UNDETERMINED for value in checks.values())
    assert assessment.signals["failed_checks"] == []


def test_boundary_values_are_inclusive_on_the_passing_side() -> None:
    """Pinned so a later tuning round cannot silently flip a boundary and start
    rejecting photos that used to be fine."""
    exact = _with(
        subject_frame_fill_ratio=MIN_SUBJECT_FRAME_FILL_RATIO,
        subject_vertical_span_ratio=MIN_SUBJECT_VERTICAL_SPAN_RATIO,
        sharpness_score=MIN_SHARPNESS_SCORE,
        pixel_width=MIN_PIXEL_WIDTH,
        pixel_height=MIN_PIXEL_HEIGHT,
    )
    assert assess_person_frame(exact).quality_status is AvatarQualityStatus.PASSED
    assert (
        assess_person_frame(
            _with(subject_frame_fill_ratio=MAX_SUBJECT_FRAME_FILL_RATIO)
        ).quality_status
        is AvatarQualityStatus.PASSED
    )


def test_every_required_check_appears_in_the_rendered_signals() -> None:
    checks = assess_person_frame(GOOD).signals["checks"]
    for required in REQUIRED_CHECKS:
        assert checks[required] == PASSED


# --- acceptance 3: the sensitive-inference deny test ----------------------


def test_the_signal_vocabulary_carries_no_judgement_about_the_person() -> None:
    """Acceptance 3, over the real rendered payload rather than a hand-written
    dict: nothing an actual assessment emits may name a person-attribute."""
    for observation in (GOOD, None, _with(subject_region_count=4)):
        signals = assess_person_frame(observation).signals
        assert set(signals) <= ALLOWED_QUALITY_SIGNAL_KEYS
        validate_quality_signals(signals)  # must not raise


@pytest.mark.parametrize(
    "forbidden_key",
    [
        "estimated_age",
        "age_band",
        "body_shape",
        "bodyshape",
        "attractiveness_score",
        "bmi",
        "waist_ratio",
        "skin_tone",
        "gender_guess",
        "ethnicity",
        "weight_kg",
        "physique",
    ],
)
def test_a_person_attribute_key_is_refused(forbidden_key: str) -> None:
    with pytest.raises(ForbiddenQualitySignalError):
        validate_quality_signals({forbidden_key: 1})


def test_a_person_attribute_hidden_in_a_nested_value_is_refused() -> None:
    """The walk has to be recursive: `checks` is a dict and `failed_checks` a
    list, so a judgement smuggled into a value would otherwise pass."""
    with pytest.raises(ForbiddenQualitySignalError):
        validate_quality_signals({"checks": {"single_subject": "passed_looks_slim"}})
    with pytest.raises(ForbiddenQualitySignalError):
        validate_quality_signals({"failed_checks": ["body_too_wide"]})


def test_an_unlisted_key_is_refused_even_when_it_names_nothing_forbidden() -> None:
    """The allowlist is the guarantee, the deny-list only the explanation.
    `estimated_height_cm` is a body measurement that the deny-list deliberately
    does not cover (`height` describes the image file), so the allowlist has to
    be what stops it."""
    with pytest.raises(ForbiddenQualitySignalError):
        validate_quality_signals({"estimated_height_cm": 170})


def test_the_deny_list_and_the_allowlist_do_not_contradict_each_other() -> None:
    """A term on the deny-list that matches an allowed key would make every
    real assessment raise. This is the test that catches a careless addition to
    either list — e.g. adding "coverage" (which contains "age") as a signal
    name, or adding "height" to the deny-list while `frame_height_px` ships."""
    for key in ALLOWED_QUALITY_SIGNAL_KEYS:
        for term in FORBIDDEN_SIGNAL_TERMS:
            assert term not in key.lower(), f"{term!r} in allowed key {key!r}"


def test_no_signal_is_a_percentage_or_a_grade_of_the_person() -> None:
    """V3-005's anti-goal, carried forward: the numbers here describe the
    photograph (fill ratio, focus, pixels), so no key may read as a score of
    its subject."""
    signals = assess_person_frame(GOOD).signals
    assert "subject_score" not in signals
    assert "quality_score" not in signals
    # `sharpness_score` is the one *_score, and it grades the optics.
    scored = [key for key in signals if key.endswith("_score")]
    assert scored == ["sharpness_score"]
