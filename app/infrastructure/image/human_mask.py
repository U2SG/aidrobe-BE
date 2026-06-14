"""Build a wanx repaint mask from a wear selfie.

Subject (person + clothes) -> BLACK (wanx preserves it pixel-for-pixel);
background -> WHITE (repainted). The subject silhouette is dilated so loose
hems / bags just past the human outline stay locked too — the spike showed
human-seg alone drops anything past the body edge into the repaint zone.

`rembg` + `onnxruntime` are OPTIONAL deps, loaded only when the OOTD-poster
feature is enabled. We raise if they're absent rather than silently falling
back to repainting the whole image — that would let the model alter the
user's real garments, defeating the whole point.
"""

from __future__ import annotations

import io
from typing import Any

# Lazy module-level singleton: rembg.new_session() loads the U2Net weights +
# spins up an onnxruntime session (seconds + memory), so we build it once and
# reuse across posters. Safe because drain processes jobs sequentially.
_session: Any = None


def _get_session() -> Any:
    global _session
    if _session is None:
        from rembg import new_session

        _session = new_session("u2net_human_seg")
    return _session


def build_subject_mask(image_bytes: bytes, *, dilate_radius: int = 3) -> bytes:
    # dilate_radius trades two failure modes: too large keeps a ring of the
    # ORIGINAL background around the subject (a dark halo on the new bg), too
    # small can clip loose hems just past the human outline. 3px is the sweet
    # spot — enough to cover clothing edges without a visible halo.
    try:
        from rembg import remove
    except ImportError as exc:  # pragma: no cover - optional dep
        raise RuntimeError(
            "rembg is required for OOTD-poster mask generation; "
            "install rembg + onnxruntime to enable ENABLE_OOTD_POSTER"
        ) from exc
    from PIL import Image, ImageFilter

    base = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    cut = remove(base, session=_get_session())  # RGBA, alpha channel = person
    alpha = cut.split()[-1].point(lambda a: 255 if a > 128 else 0)
    if dilate_radius > 0:
        alpha = alpha.filter(ImageFilter.MaxFilter(dilate_radius * 2 + 1))

    mask = Image.new("RGB", base.size, (255, 255, 255))  # white = repaint bg
    mask.paste(Image.new("RGB", base.size, (0, 0, 0)), (0, 0), alpha)  # black = keep subject
    out = io.BytesIO()
    mask.save(out, format="PNG")
    return out.getvalue()
