"""V3-018 — the only `VisualQualityGatePort` adapter in the tree.

Contract §6.4: development and CI use fake/recorded adapters only. Like
`FakeStaticTryOn`, this is not a stand-in for something missing — for P0 it *is*
the gate, and replacing its measurements with a comparison model is part of the
human-gated benchmark review ("final visual quality threshold requires
benchmark review before production").

**Local, deterministic, and it leaves the machine never.** Given the same bytes
it returns an identical observation, which is what lets the retry-cap tests
count renders instead of hoping, and what keeps a cached asset's persisted
verdict reproducible.

**What it measures, and what it therefore cannot prove.** Three cheap, real
measurements of the rendered file: does it decode, how big is it, and does it
have any tonal variation at all. Those catch the silent provider failures that
actually occur — truncated bytes, a thumbnail, a blank or black frame — and
they are honest, because they are measurements a real pipeline needs too. What
they cannot see is whether the person still looks like the person or the
garment still looks like the garment; the rules layer reports those dimensions
`undetermined` rather than letting this adapter pretend. The `request` argument
arrives so a future comparison-model adapter has the source images to compare
against without changing the port; this adapter deliberately ignores it.

**Nothing here retains or emits image data.** The bytes are decoded, measured
and dropped; the observation carries numbers and a slug (contract §7, and
V3-018 acceptance 4).
"""

from __future__ import annotations

import io

from PIL import Image, ImageStat

from app.application.ports.static_tryon import StaticTryOnRequest
from app.application.ports.visual_quality_gate import RenderQualityObservation

CHECKER = "fake-visual-quality-gate-1"

#: `ImageStat.stddev` of an 8-bit grayscale frame is 0 for a flat image and
#: tops out near 127.5 (half the frame black, half white), so dividing by it
#: lands the measure in [0, 1] with the two failure modes at the ends.
_MAX_GRAYSCALE_STDDEV = 127.5


class FakeVisualQualityGate:
    """Deterministic local measurement. No network, no provider, no spend."""

    async def observe(
        self, rendered_image: bytes, request: StaticTryOnRequest
    ) -> RenderQualityObservation:
        try:
            with Image.open(io.BytesIO(rendered_image)) as decoded:
                grayscale = decoded.convert("L")
                width, height = decoded.size
        except Exception:
            # Not an image. That is a determination about the render — the
            # bytes were examined and they do not decode — not a checker
            # malfunction, so it reports `decode_ok=False` rather than raising
            # into the worker's technical-failure path.
            return RenderQualityObservation(decode_ok=False, checker=CHECKER)

        stddev = ImageStat.Stat(grayscale).stddev[0]
        return RenderQualityObservation(
            decode_ok=True,
            pixel_width=width,
            pixel_height=height,
            tonal_range=min(stddev / _MAX_GRAYSCALE_STDDEV, 1.0),
            checker=CHECKER,
        )
