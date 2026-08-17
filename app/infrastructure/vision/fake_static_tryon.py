"""V3-016 — the only `StaticTryOnPort` adapter in the tree.

Contract §6.4: "Development and CI use fake/recorded adapters only; any
real-provider evaluation is a separately budgeted, explicitly authorized task
(V3-016, human gate). Real user photos are never sent to third-party providers
during development loops." So this adapter is not a stand-in for something
missing — for P0 it *is* the implementation, and adding a real provider is a
human-gated decision rather than a follow-up chore. That gate is this feature's
own `human_gate`: "Production provider selection is out of scope."

**Local, deterministic, and it leaves the machine never.** Given the same request
it returns byte-identical bytes, which is what lets V3-017 assert a real
`queued → ready` transition, and V3-020 render a real preview, with no container,
no network and no spend.

**Order-insensitive by construction, because the cache key is.** Contract §6.3
keys a preview on `sorted_supporting_item_ids` — two requests that differ only in
the order of the supporting garments are the *same* cached asset. A renderer
whose output depended on that order would therefore return an image that a
re-render could not reproduce, and V3-018's quality verdict would be attached to
one arrangement while a later cache hit served another. So supporting garments
are sorted here (slot order, then content digest) rather than rendered in call
order, and `test_supporting_garment_order_does_not_change_the_render` pins it.

**What this composites, and what it therefore cannot prove.** The transform is a
normalization a real pipeline would also need — decode, drop EXIF (which carries
GPS and device identifiers a derived asset has no reason to keep), letterbox onto
a fixed canvas so every preview has the same geometry, place the garments in a
fixed layout, re-encode as PNG. It arranges real pixels; it does not *generate* a
person wearing clothes. So a test that sees a preview is seeing the caller's own
bytes flow through the port, through storage and onto an asset row — the whole
lifecycle is exercised for real. What is *not* proven anywhere is that a
production model produces a believable try-on. That is the budgeted, human-gated
provider evaluation, and it is the same honest limit V3-013 recorded for
`FakeAvatarPreparation` and V3-012 for `AVATAR_PERSON_DETECTOR=unavailable`.
"""

from __future__ import annotations

import hashlib
import io

from PIL import Image

from app.application.ports.static_tryon import (
    GarmentImage,
    GarmentSlot,
    PipelineIdentity,
    StaticTryOnRequest,
    StaticTryOnResult,
)

#: Canonical canvas for a preview. Portrait 3:4, matching
#: `fake_avatar_preparation.CANONICAL_SIZE` so the avatar render arrives at its
#: native geometry and is not resampled twice.
PREVIEW_SIZE = (768, 1024)

#: Edge of one square garment tile in the strip.
_TILE = 160

#: Flat neutral backdrop. Mid-grey rather than pure white or black so a preview
#: is visibly a *generated asset* and not mistaken for a photograph.
_BACKDROP = (128, 128, 128)

#: The strip's tiles sit on a slightly lighter panel so a garment with a pale
#: background is still distinguishable from the canvas behind it.
_PANEL = (150, 150, 150)

PROVIDER = "fake"
MODEL_VERSION = "fake-static-tryon-1"
#: Bump when the layout or the normalization changes. It is part of contract
#: §6.3's strong cache key, so a bump is what makes already-rendered previews
#: eligible for a fresh run instead of being permanently pinned to an older
#: pipeline — the same reasoning as `fake_avatar_preparation.PROMPT_VERSION` and
#: V3-012's `assessor` key component.
PROMPT_VERSION = "v1"

#: Slot order for the strip. Top-down the way an outfit reads, with `outer` last
#: because it is the optional layer.
_SLOT_ORDER: dict[GarmentSlot, int] = {
    GarmentSlot.TORSO: 0,
    GarmentSlot.LEGS: 1,
    GarmentSlot.FEET: 2,
    GarmentSlot.OUTER: 3,
}


def _sort_key(garment: GarmentImage) -> tuple[int, str]:
    """Deterministic, content-addressed and independent of call order.

    Two garments in the same slot are ordered by the digest of their bytes, so
    the arrangement is a function of the *set* of garments rather than of how the
    caller happened to list them — see the module docstring on §6.3.
    """
    return (
        _SLOT_ORDER[garment.slot],
        hashlib.sha256(garment.image).hexdigest(),
    )


def _flatten(decoded: Image.Image, background: tuple[int, int, int]) -> Image.Image:
    """Drop the alpha channel *against the backdrop*, not against black.

    `Image.convert("RGB")` on an RGBA image composites onto black, so a
    transparent cutout would arrive as a black silhouette — and wardrobe item
    images are cutouts, so that is the normal case here rather than an edge one.
    Pasting through the alpha band is what actually honours the background, which
    `test_a_transparent_cutout_does_not_render_as_a_black_silhouette` pins.
    """
    if decoded.mode not in ("RGBA", "LA", "PA", "P"):
        return decoded.convert("RGB")
    rgba = decoded.convert("RGBA")
    flattened = Image.new("RGB", rgba.size, background)
    flattened.paste(rgba, mask=rgba.getchannel("A"))
    return flattened


def _fit(source: bytes, box: tuple[int, int], background: tuple[int, int, int]) -> Image.Image:
    """Decode, flatten and letterbox into `box`. No metadata survives."""
    with Image.open(io.BytesIO(source)) as decoded:
        frame = _flatten(decoded, background)
        frame.thumbnail(box, Image.Resampling.LANCZOS)
        # `Image.new` starts clean and nothing copies the source's EXIF onto it.
        canvas = Image.new("RGB", box, background)
        canvas.paste(frame, ((box[0] - frame.width) // 2, (box[1] - frame.height) // 2))
        return canvas


def _compose(request: StaticTryOnRequest) -> bytes:
    canvas = _fit(request.avatar_image, PREVIEW_SIZE, _BACKDROP)

    # Candidate first, unconditionally: it is the garment the decision is about,
    # so it occupies the top tile whatever slot it fills. The supporting items
    # follow in sorted order.
    garments = [request.candidate_garment, *sorted(request.supporting_garments, key=_sort_key)]
    for index, garment in enumerate(garments):
        top = index * _TILE
        if top + _TILE > PREVIEW_SIZE[1]:
            # The strip is bounded by the canvas. V3-006 composes at most four
            # slots, so six tiles is already generous headroom; silently running
            # off the bottom would be worse than stopping.
            break
        canvas.paste(_fit(garment.image, (_TILE, _TILE), _PANEL), (PREVIEW_SIZE[0] - _TILE, top))

    buffer = io.BytesIO()
    # `optimize` keeps the encoding stable across runs for the same pixels.
    canvas.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


class FakeStaticTryOn:
    """Deterministic local composition. No network, no provider, no spend."""

    @property
    def pipeline_identity(self) -> PipelineIdentity:
        return PipelineIdentity(
            provider=PROVIDER,
            model_version=MODEL_VERSION,
            prompt_version=PROMPT_VERSION,
        )

    async def render(self, request: StaticTryOnRequest) -> StaticTryOnResult:
        return StaticTryOnResult(image=_compose(request), identity=self.pipeline_identity)
