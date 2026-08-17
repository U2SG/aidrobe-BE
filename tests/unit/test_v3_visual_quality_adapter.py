"""V3-018 — `FakeVisualQualityGate` measures real pixels, deterministically.

The adapter is the only `VisualQualityGatePort` implementation in the tree
(contract §6.4), so what it measures IS what P0's gate can see. These tests run
it against synthesized images — no committed fixtures, no real photograph, the
V3-013/016 rule held one step earlier than "do not send photos to a provider".

The last test is the one that ties the feature together: the P0 renderer's own
output, judged by the P0 gate through the real rules, is SHOW. Without it, a
threshold tuned past what `FakeStaticTryOn` produces would reject every render
in every environment and nothing in tests/unit would notice.
"""

from __future__ import annotations

import io

from PIL import Image

from app.application.ports.static_tryon import (
    GarmentImage,
    GarmentSlot,
    PoseTemplate,
    StaticTryOnRequest,
    ViewRequirement,
)
from app.application.purchase.visual_quality_rules import (
    MIN_TONAL_RANGE,
    RenderVerdict,
    evaluate_render,
)
from app.infrastructure.vision.fake_static_tryon import FakeStaticTryOn
from app.infrastructure.vision.fake_visual_quality_gate import CHECKER, FakeVisualQualityGate


def _png(size: tuple[int, int] = (768, 1024), color: tuple[int, int, int] = (170, 150, 130)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _two_tone_png(size: tuple[int, int] = (768, 1024)) -> bytes:
    """Half dark, half light — a frame with unmistakable tonal variation."""
    image = Image.new("RGB", size, (40, 40, 40))
    image.paste(Image.new("RGB", (size[0], size[1] // 2), (215, 215, 215)), (0, 0))
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _request() -> StaticTryOnRequest:
    """A syntactically real request; the adapter deliberately ignores it.

    The avatar is 600×900 — the services harness's shape, NOT the canvas's own
    aspect. That matters for the last test: a solid-color avatar that exactly
    tiles the canvas produces a render that is one flat gray from edge to edge
    (the letterbox backdrop never shows), which the gate correctly hides. Real
    inputs are never single flat colors; the fixture must not be more
    degenerate than the degenerate case the gate exists to catch.
    """
    return StaticTryOnRequest(
        avatar_image=_png((600, 900)),
        candidate_garment=GarmentImage(image=_png((300, 300)), slot=GarmentSlot.OUTER),
        supporting_garments=(GarmentImage(image=_png((300, 300)), slot=GarmentSlot.TORSO),),
        pose_template=PoseTemplate.NEUTRAL_FULL_BODY,
        view=ViewRequirement.FRONT,
    )


async def test_the_same_bytes_measure_identically() -> None:
    """Determinism is what lets the retry-cap tests count renders instead of
    hoping, and what keeps a cached asset's persisted verdict reproducible."""
    gate = FakeVisualQualityGate()
    image = _two_tone_png()

    first = await gate.observe(image, _request())
    second = await gate.observe(image, _request())

    assert first == second


async def test_bytes_that_are_not_an_image_are_a_determination_not_a_crash() -> None:
    """The bytes were examined and they do not decode — that is `decode_ok=
    False`, a verdict input, not a checker malfunction that should raise into
    the worker's technical-failure path."""
    observation = await FakeVisualQualityGate().observe(b"not a png at all", _request())

    assert observation.decode_ok is False
    assert observation.pixel_width is None
    assert observation.pixel_height is None
    assert observation.tonal_range is None
    assert observation.checker == CHECKER


async def test_a_flat_frame_measures_zero_tonal_range() -> None:
    """The classic silent provider failure: 200 OK, valid PNG, nothing in it."""
    observation = await FakeVisualQualityGate().observe(_png(), _request())

    assert observation.decode_ok is True
    assert (observation.pixel_width, observation.pixel_height) == (768, 1024)
    assert observation.tonal_range == 0.0


async def test_a_frame_with_content_measures_above_the_floor() -> None:
    observation = await FakeVisualQualityGate().observe(_two_tone_png(), _request())

    assert observation.tonal_range is not None
    assert observation.tonal_range > MIN_TONAL_RANGE
    assert observation.tonal_range <= 1.0


async def test_dimensions_are_the_decoded_files_not_an_assumption() -> None:
    observation = await FakeVisualQualityGate().observe(_png((200, 300)), _request())

    assert (observation.pixel_width, observation.pixel_height) == (200, 300)


async def test_the_p0_pipelines_own_render_passes_the_p0_gate() -> None:
    """The whole chain, no database: FakeStaticTryOn renders, the gate
    measures, the rules say SHOW. A threshold raised past what the fake
    produces — or a fake degraded below what the threshold demands — fails
    here on the day it happens, not in a services run's rejected row."""
    render = await FakeStaticTryOn().render(_request())
    observation = await FakeVisualQualityGate().observe(render.image, _request())

    decision = evaluate_render(observation)

    assert decision.verdict is RenderVerdict.SHOW
    assert decision.signals["failed_checks"] == []
