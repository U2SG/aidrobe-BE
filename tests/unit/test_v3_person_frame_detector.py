"""V3-012 — the pure half of the person-frame detector, on synthesized images.

`summarize_subject_mask` and `measure_sharpness` are plain functions over image
bytes, so this file drives them with PIL-drawn masks: no container, no rembg, no
model weights, no network. Runs under `--noconftest`.

What is NOT covered here, stated rather than implied: `MaskPersonFrameDetector`
calls the optional rembg/u2net model, which is absent in CI. Whether that model
finds a person in a real photograph is not asserted anywhere in this suite — only
that a mask, once produced, is interpreted correctly.
"""

import io

import pytest
from PIL import Image, ImageDraw

from app.application.avatar.candidate_rules import MIN_SHARPNESS_SCORE
from app.infrastructure.vision.person_frame_detector import (
    MIN_REGION_FILL_RATIO,
    SUBJECT_LUMA_THRESHOLD,
    measure_sharpness,
    summarize_subject_mask,
)

FRAME = (200, 400)


def _mask(*boxes: tuple[int, int, int, int], size: tuple[int, int] = FRAME) -> bytes:
    """A mask in `build_subject_mask`'s polarity: subject BLACK on WHITE."""
    image = Image.new("L", size, color=255)
    draw = ImageDraw.Draw(image)
    for box in boxes:
        draw.rectangle(box, fill=0)
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


# --- region counting -------------------------------------------------------


def test_one_standing_figure_is_one_region() -> None:
    summary = summarize_subject_mask(_mask((70, 40, 130, 360)))
    assert summary["subject_region_count"] == 1
    assert 0.15 < summary["subject_frame_fill_ratio"] < 0.35
    assert summary["subject_vertical_span_ratio"] > 0.75
    assert summary["top_edge_contact_ratio"] == 0.0


def test_two_separated_figures_are_two_regions() -> None:
    """The single-subject check's whole basis. Boxes are far apart so the
    downscale to 128px cannot merge them."""
    summary = summarize_subject_mask(_mask((10, 40, 60, 360), (140, 40, 190, 360)))
    assert summary["subject_region_count"] == 2


def test_an_empty_mask_reports_nothing_found() -> None:
    summary = summarize_subject_mask(_mask())
    assert summary["subject_region_count"] == 0
    assert summary["subject_frame_fill_ratio"] == 0.0
    assert summary["subject_vertical_span_ratio"] == 0.0


def test_segmentation_speckle_is_not_counted_as_a_second_subject() -> None:
    """Without the floor, one stray blob makes a good photo read as "two
    people" and get rejected. A 2x2 box is far below MIN_REGION_FILL_RATIO."""
    summary = summarize_subject_mask(_mask((70, 40, 130, 360), (2, 2, 4, 4)))
    assert summary["subject_region_count"] == 1
    assert MIN_REGION_FILL_RATIO > 0


def test_speckle_does_not_inflate_the_bounding_box() -> None:
    """The stated reason the discarded regions are excluded from the
    measurements too: a corner pixel would otherwise stretch the span to the
    whole frame and make the framing check unfalsifiable."""
    clean = summarize_subject_mask(_mask((70, 200, 130, 300)))
    speckled = summarize_subject_mask(_mask((70, 200, 130, 300), (1, 1, 3, 3)))
    assert speckled["subject_vertical_span_ratio"] == clean["subject_vertical_span_ratio"]
    assert speckled["subject_frame_fill_ratio"] == clean["subject_frame_fill_ratio"]


# --- framing ---------------------------------------------------------------


def test_a_subject_running_off_the_top_is_measured_at_the_top_edge() -> None:
    """The "cut off above the head" signal — the §7.4 完整 failure that no later
    step can repair."""
    summary = summarize_subject_mask(_mask((40, 0, 160, 360)))
    assert summary["top_edge_contact_ratio"] > 0.5


def test_a_full_frame_mask_saturates_fill_and_span() -> None:
    summary = summarize_subject_mask(_mask((0, 0, FRAME[0], FRAME[1])))
    assert summary["subject_frame_fill_ratio"] > 0.95
    assert summary["subject_vertical_span_ratio"] > 0.95
    assert summary["top_edge_contact_ratio"] > 0.95


def test_a_small_distant_subject_has_a_low_fill_ratio() -> None:
    """Survives the speckle floor but is far too small to be a usable whole-body
    reference — so it is counted, and the framing threshold is what refuses it."""
    summary = summarize_subject_mask(_mask((85, 150, 115, 260)))
    assert summary["subject_region_count"] == 1
    assert 0.0 < summary["subject_frame_fill_ratio"] < 0.05


def test_a_subject_below_the_speckle_floor_reads_as_nobody_found() -> None:
    """The documented interaction between the two floors, pinned so it is a
    known behaviour rather than a surprise: a subject smaller than
    MIN_REGION_FILL_RATIO is discarded as noise, so the mask reports zero
    regions. Both paths end in a rejection — via the single-subject check here
    instead of the framing check — but the reported reason differs, and
    `subject_region_count == 0` is what the rule layer reads."""
    summary = summarize_subject_mask(_mask((95, 190, 105, 230)))
    assert summary["subject_region_count"] == 0


def test_measurements_are_ratios_so_they_do_not_depend_on_resolution() -> None:
    """Two photos of the same composition at different sizes must produce the
    same framing numbers — otherwise the thresholds would mean different things
    to different cameras."""
    small = summarize_subject_mask(_mask((25, 10, 75, 190), size=(100, 200)))
    large = summarize_subject_mask(_mask((100, 40, 300, 760), size=(400, 800)))
    assert abs(small["subject_frame_fill_ratio"] - large["subject_frame_fill_ratio"]) < 0.03
    assert abs(small["subject_vertical_span_ratio"] - large["subject_vertical_span_ratio"]) < 0.03


def test_the_polarity_is_subject_dark_on_light_background() -> None:
    """Pinned because reading it backwards would invert every verdict: an empty
    background would look like a frame-filling subject."""
    assert SUBJECT_LUMA_THRESHOLD == 128
    inverted = Image.new("L", FRAME, color=0)  # all "subject"
    assert summarize_subject_mask(_png(inverted))["subject_frame_fill_ratio"] > 0.95
    blank = Image.new("L", FRAME, color=255)  # all background
    assert summarize_subject_mask(_png(blank))["subject_region_count"] == 0


# --- sharpness -------------------------------------------------------------


def test_a_detailed_image_scores_higher_than_a_flat_one() -> None:
    flat = Image.new("RGB", FRAME, color=(128, 128, 128))
    detailed = Image.new("RGB", FRAME, color=(0, 0, 0))
    draw = ImageDraw.Draw(detailed)
    for x in range(0, FRAME[0], 4):
        draw.rectangle((x, 0, x + 1, FRAME[1]), fill=(255, 255, 255))

    flat_score, _, _ = measure_sharpness(_png(flat))
    detailed_score, _, _ = measure_sharpness(_png(detailed))
    assert flat_score == 0.0
    assert detailed_score > flat_score


@pytest.mark.parametrize("size", [(200, 400), (1080, 1920), (640, 640)])
def test_a_uniform_frame_is_not_sharp(size: tuple[int, int]) -> None:
    """Regression for a real defect in the first cut of this module.

    `FIND_EDGES` is an unpadded 3x3 convolution, so it writes its own kernel
    response into the outermost 1px ring of every image. Measured over the full
    frame, a completely uniform photo scored 0.29-0.35 — above
    MIN_SHARPNESS_SCORE — which made 清晰度足够 impossible to fail and would have
    let a blank or badly blurred photo be offered as person reference material.
    Parametrized over three shapes because the artifact's size depends on the
    border's share of the frame, so a single size could pass by luck.
    """
    score, _, _ = measure_sharpness(_png(Image.new("RGB", size, color=(128, 128, 128))))
    assert score == 0.0
    assert MIN_SHARPNESS_SCORE > 0.0


def test_a_heavily_blurred_photo_falls_below_the_usable_threshold() -> None:
    """The other half of that regression: not just "blurrier than sharp" (which
    the old full-frame measure also satisfied) but actually *below the threshold
    the rule layer uses*. This is what makes the check non-vacuous."""
    from PIL import ImageFilter

    base = Image.new("RGB", (1080, 1920), color=(60, 70, 90))
    draw = ImageDraw.Draw(base)
    draw.ellipse((300, 300, 780, 1600), fill=(200, 180, 160))
    draw.rectangle((400, 700, 700, 1400), fill=(40, 60, 120))

    sharp_score, _, _ = measure_sharpness(_png(base))
    blurred_score, _, _ = measure_sharpness(_png(base.filter(ImageFilter.GaussianBlur(radius=12))))
    assert sharp_score >= MIN_SHARPNESS_SCORE
    assert blurred_score < MIN_SHARPNESS_SCORE


def test_an_image_too_small_to_have_an_interior_scores_zero_rather_than_raising() -> None:
    """A 2px image is all border. Returning 0.0 keeps the resolution check the
    reason it is refused, instead of crashing the worker on a degenerate file."""
    score, width, height = measure_sharpness(_png(Image.new("RGB", (2, 2), (128, 128, 128))))
    assert score == 0.0
    assert (width, height) == (2, 2)


def test_sharpness_is_bounded_to_the_unit_interval() -> None:
    noisy = Image.new("RGB", FRAME, color=(0, 0, 0))
    draw = ImageDraw.Draw(noisy)
    for x in range(0, FRAME[0], 2):
        for y in range(0, FRAME[1], 2):
            draw.point((x, y), fill=(255, 255, 255))
    score, _, _ = measure_sharpness(_png(noisy))
    assert 0.0 <= score <= 1.0


def test_measure_sharpness_reports_the_original_file_dimensions() -> None:
    """Not the normalized analysis copy's — the resolution check reads these, so
    returning the internal 512px copy's size would fail every large photo."""
    _score, width, height = measure_sharpness(_png(Image.new("RGB", (1080, 1920), (10, 20, 30))))
    assert (width, height) == (1080, 1920)


def test_a_blurred_photo_scores_below_its_sharp_original() -> None:
    """The discriminating pair for 清晰度足够, rather than an absolute number:
    the same content, one pass of blur apart."""
    from PIL import ImageFilter

    sharp = Image.new("RGB", FRAME, color=(0, 0, 0))
    draw = ImageDraw.Draw(sharp)
    for x in range(0, FRAME[0], 8):
        draw.rectangle((x, 0, x + 3, FRAME[1]), fill=(255, 255, 255))
    blurred = sharp.filter(ImageFilter.GaussianBlur(radius=4))

    sharp_score, _, _ = measure_sharpness(_png(sharp))
    blurred_score, _, _ = measure_sharpness(_png(blurred))
    assert blurred_score < sharp_score
