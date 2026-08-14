"""V3-012 — measure a photo's subject region. No third party, no network.

Two pieces, split so the interesting half is testable in CI:

- `summarize_subject_mask` / `measure_sharpness` are **pure** functions over
  image bytes. All of the interpretation lives here, and tests/unit drives them
  with PIL-synthesized images — no container, no model weights, no download.
- `MaskPersonFrameDetector` is a thin adapter that calls the repository's
  existing `build_subject_mask` (rembg `u2net_human_seg`, already the local
  human-segmentation tool behind the V4 OOTD poster) and hands the result to
  those pure functions. It contains no logic worth testing that the pure half
  does not already cover.

**Nothing here leaves the machine.** Segmentation runs locally against local
weights, which is what lets V3-012 assess real user photos at all: CLAUDE.md
forbids sending real user photos to a third-party provider during development
loops, and a hosted vision API would also be a provider choice — a human gate.

`rembg` + `onnxruntime` are optional runtime dependencies (they are not in the
project's install set and are absent in CI). When they are missing the adapter
raises, callers treat that as "not determined", and no photo is ever *rejected*
because the tooling was unavailable. `UnavailablePersonFrameDetector` makes
that the explicit, configured behaviour rather than an accident.
"""

import io
from collections import deque
from typing import Any

from PIL import Image, ImageFilter, ImageStat

from app.application.ports.avatar_detection import PersonFrameObservation

DETECTOR_HUMAN_SEG = "human_seg_mask"
DETECTOR_UNAVAILABLE = "unavailable"

#: Longest side the mask is reduced to before it is interpreted. Region finding
#: is O(pixels) and a person's silhouette survives this scale intact, so the
#: whole summary costs well under a millisecond regardless of camera megapixels.
MASK_ANALYSIS_LONG_SIDE = 128
#: A region smaller than this share of the frame is speckle from the segmenter,
#: not a second subject. Without a floor, mask noise would read as "two people"
#: and reject perfectly good photos.
MIN_REGION_FILL_RATIO = 0.01
#: Pixels below this are foreground: `build_subject_mask` paints the subject
#: BLACK on a WHITE background.
SUBJECT_LUMA_THRESHOLD = 128
#: Long side the image is normalized to before focus is measured, so the score
#: describes the picture rather than its resolution.
SHARPNESS_ANALYSIS_LONG_SIDE = 512
#: Edge-magnitude standard deviation treated as "fully sharp". Anything at or
#: above this normalizes to 1.0. Calibration constant, not a measurement.
SHARPNESS_REFERENCE_STDDEV = 45.0


def measure_sharpness(image_bytes: bytes) -> tuple[float, int, int]:
    """Return `(sharpness_score, pixel_width, pixel_height)`.

    The score is the standard deviation of edge magnitude over a size-normalized
    greyscale copy, squashed into [0, 1]. It is a relative focus measure of the
    *photograph*; it says nothing about its subject. The dimensions returned are
    the original file's, not the normalized copy's.

    The outermost 1px ring is discarded before the statistic is taken, and that
    is load-bearing rather than tidiness. `FIND_EDGES` is a 3x3 convolution with
    no padding, so it writes the kernel's edge response into that ring for every
    image — a completely uniform frame comes back with a stddev of ~13-15 (score
    0.29-0.35) from the border alone, which is ABOVE `MIN_SHARPNESS_SCORE`.
    Measured over the full frame, 清晰度足够 could therefore never fail: a blank
    or heavily blurred photo would be graded `passed` and offered as person
    reference material. Cropped, the same frames measure 0.0 and sharp content
    measures 20-127, so the threshold discriminates. `test_a_uniform_frame_is_not
    _sharp` pins the regression.
    """
    with Image.open(io.BytesIO(image_bytes)) as opened:
        width, height = opened.size
        grey = opened.convert("L")
    normalized = _fit_long_side(grey, SHARPNESS_ANALYSIS_LONG_SIDE)
    edges = _without_border(normalized.filter(ImageFilter.FIND_EDGES))
    if edges is None:
        # 1 or 2 pixels on a side: nothing but border. The resolution check
        # rejects this size anyway; refusing to invent a focus measure keeps
        # that the reason it fails.
        return 0.0, width, height
    stddev = float(ImageStat.Stat(edges).stddev[0])
    score = max(0.0, min(1.0, stddev / SHARPNESS_REFERENCE_STDDEV))
    return score, width, height


def _without_border(image: Any) -> Any | None:
    """Drop the 1px ring `FIND_EDGES` fills with its own kernel artifact."""
    width, height = image.size
    if width <= 2 or height <= 2:
        return None
    return image.crop((1, 1, width - 1, height - 1))


def summarize_subject_mask(mask_bytes: bytes) -> dict[str, float | int]:
    """Interpret a subject mask into neutral framing measurements.

    Expects the polarity `build_subject_mask` produces: subject dark, background
    light. Returns `subject_region_count`, `subject_frame_fill_ratio`,
    `subject_vertical_span_ratio` and `top_edge_contact_ratio`.

    Regions are found by flood fill on 4-connectivity over a downscaled grid.
    Anything below `MIN_REGION_FILL_RATIO` is discarded as segmentation
    speckle before counting, and the reported fill / span / edge-contact
    numbers are computed from the surviving regions only — otherwise a stray
    pixel in a corner would inflate the bounding box to the whole frame and
    make the framing check unfalsifiable.
    """
    with Image.open(io.BytesIO(mask_bytes)) as opened:
        grey = opened.convert("L")
    grid = _fit_long_side(grey, MASK_ANALYSIS_LONG_SIDE)
    width, height = grid.size
    total = width * height
    if total == 0:
        return {
            "subject_region_count": 0,
            "subject_frame_fill_ratio": 0.0,
            "subject_vertical_span_ratio": 0.0,
            "top_edge_contact_ratio": 0.0,
        }

    # `tobytes()` rather than `getdata()`: for mode "L" it is the same
    # row-major byte sequence, but `getdata` is deprecated in Pillow 12 and
    # `get_flattened_data` does not exist in older ones.
    foreground = [value < SUBJECT_LUMA_THRESHOLD for value in grid.tobytes()]
    minimum_region = max(1, int(total * MIN_REGION_FILL_RATIO))

    regions = _find_regions(foreground, width, height, minimum_region)
    if not regions:
        return {
            "subject_region_count": 0,
            "subject_frame_fill_ratio": 0.0,
            "subject_vertical_span_ratio": 0.0,
            "top_edge_contact_ratio": 0.0,
        }

    kept = {index for region in regions for index in region}
    filled = len(kept)
    rows = [index // width for index in kept]
    top_row_hits = sum(1 for index in kept if index < width)
    return {
        "subject_region_count": len(regions),
        "subject_frame_fill_ratio": filled / total,
        "subject_vertical_span_ratio": (max(rows) - min(rows) + 1) / height,
        "top_edge_contact_ratio": top_row_hits / width,
    }


def _find_regions(
    foreground: list[bool], width: int, height: int, minimum_region: int
) -> list[list[int]]:
    """4-connected components of `foreground`, keeping only the large ones."""
    seen = [False] * len(foreground)
    regions: list[list[int]] = []
    for start in range(len(foreground)):
        if not foreground[start] or seen[start]:
            continue
        seen[start] = True
        queue = deque([start])
        component: list[int] = []
        while queue:
            index = queue.popleft()
            component.append(index)
            row, column = divmod(index, width)
            if column > 0:
                _visit(index - 1, foreground, seen, queue)
            if column < width - 1:
                _visit(index + 1, foreground, seen, queue)
            if row > 0:
                _visit(index - width, foreground, seen, queue)
            if row < height - 1:
                _visit(index + width, foreground, seen, queue)
        if len(component) >= minimum_region:
            regions.append(component)
    return regions


def _visit(index: int, foreground: list[bool], seen: list[bool], queue: deque[int]) -> None:
    if foreground[index] and not seen[index]:
        seen[index] = True
        queue.append(index)


def _fit_long_side(image: Any, long_side: int) -> Any:
    width, height = image.size
    longest = max(width, height)
    if longest <= long_side or longest == 0:
        return image
    scale = long_side / longest
    size = (max(1, round(width * scale)), max(1, round(height * scale)))
    return image.resize(size, Image.Resampling.BOX)


class MaskPersonFrameDetector:
    """Local human-segmentation adapter. Raises when its optional deps are absent."""

    async def observe(self, image_bytes: bytes) -> PersonFrameObservation:
        from app.infrastructure.image.human_mask import build_subject_mask

        sharpness, width, height = measure_sharpness(image_bytes)
        # `dilate_radius=0`: the OOTD poster inflates the silhouette so loose
        # hems stay out of the repaint zone, but here inflation would merge two
        # people standing close together into one region and defeat the
        # single-subject check outright.
        mask = build_subject_mask(image_bytes, dilate_radius=0)
        summary = summarize_subject_mask(mask)
        return PersonFrameObservation(
            subject_region_count=int(summary["subject_region_count"]),
            subject_frame_fill_ratio=float(summary["subject_frame_fill_ratio"]),
            subject_vertical_span_ratio=float(summary["subject_vertical_span_ratio"]),
            top_edge_contact_ratio=float(summary["top_edge_contact_ratio"]),
            sharpness_score=sharpness,
            pixel_width=width,
            pixel_height=height,
            detector=DETECTOR_HUMAN_SEG,
        )


class UnavailablePersonFrameDetector:
    """Determines nothing, on purpose.

    Selected when an operator has not enabled local segmentation. Every photo
    comes back fully undetermined, so material stays `pending` — unassessed,
    never offered and never refused.
    """

    async def observe(self, image_bytes: bytes) -> PersonFrameObservation:
        return PersonFrameObservation(detector=DETECTOR_UNAVAILABLE)
