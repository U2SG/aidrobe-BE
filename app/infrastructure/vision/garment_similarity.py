"""Free local pre-screen for same-garment checks, run before any vision-model call.

Answers only the clear cases and leaves the rest (None) to the model:
- crops from two uploads of the very same photo (same fingerprint) that are
  near-identical pictures → same garment;
- crops from different photos whose colours or proportions are far apart →
  different garments. A wrong "different" only leaves an extra row for the
  user to merge, so this side may be somewhat aggressive; a wrong "same" would
  silently merge two garments, so "same" is never decided on colour alone.
"""

from __future__ import annotations

import io

from PIL import Image, ImageOps

# Thresholds calibrated on 72 real cross-photo crop pairs (E1 album eval): the
# "different" cut removed 22 of 43 different pairs and 1 of 29 same pairs (a
# pair that still merged through a third photo). Keep them on the cautious side.

#: HSV histogram bins (hue, saturation, value) over the crop's central region.
HIST_BINS = (8, 3, 3)
#: Share of each side kept around the centre, so background and neighbouring
#: garments at the crop edges weigh less than the garment itself.
CENTER_FRACTION = 0.4
#: L1 distance (0..2) between normalised histograms above which two crops are
#: treated as different garments without asking the model.
DIFFERENT_HIST_DISTANCE = 1.9
#: Width/height ratios further apart than this factor are different garments.
DIFFERENT_ASPECT_FACTOR = 3.0
#: dHash (64-bit) bit distance at or below which two crops of the same photo
#: (re-upload) are the same picture of the same garment.
SAME_PHOTO_HASH_DISTANCE = 6


def _open(data: bytes) -> Image.Image:
    return ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")


def _center(image: Image.Image) -> Image.Image:
    width, height = image.size
    dx, dy = int(width * (1 - CENTER_FRACTION) / 2), int(height * (1 - CENTER_FRACTION) / 2)
    return image.crop((dx, dy, width - dx, height - dy)) if width - 2 * dx > 0 and height - 2 * dy > 0 else image


def color_histogram(image: Image.Image) -> list[float]:
    small = _center(image).resize((64, 64)).convert("HSV")
    h_bins, s_bins, v_bins = HIST_BINS
    counts = [0] * (h_bins * s_bins * v_bins)
    raw = small.tobytes()
    for offset in range(0, len(raw), 3):
        h, s, v = raw[offset], raw[offset + 1], raw[offset + 2]
        index = (h * h_bins // 256) * s_bins * v_bins + (s * s_bins // 256) * v_bins + v * v_bins // 256
        counts[index] += 1
    total = sum(counts) or 1
    return [count / total for count in counts]


def histogram_distance(a: list[float], b: list[float]) -> float:
    return sum(abs(x - y) for x, y in zip(a, b, strict=True))


def dhash(image: Image.Image) -> int:
    gray = image.convert("L").resize((9, 8))
    pixels = list(gray.getdata())
    value = 0
    for row in range(8):
        for col in range(8):
            value = (value << 1) | (pixels[row * 9 + col] > pixels[row * 9 + col + 1])
    return value


def _aspect(image: Image.Image) -> float:
    width, height = image.size
    return width / height if height else 1.0


def prescreen(crop_a: bytes, crop_b: bytes, same_photo: bool) -> bool | None:
    """True / False when the answer is clear without the model, else None."""
    try:
        a, b = _open(crop_a), _open(crop_b)
    except Exception:
        return None
    if same_photo:
        return True if bin(dhash(a) ^ dhash(b)).count("1") <= SAME_PHOTO_HASH_DISTANCE else None
    ratio = _aspect(a) / _aspect(b)
    if max(ratio, 1 / ratio) > DIFFERENT_ASPECT_FACTOR:
        return False
    if histogram_distance(color_histogram(a), color_histogram(b)) > DIFFERENT_HIST_DISTANCE:
        return False
    return None
