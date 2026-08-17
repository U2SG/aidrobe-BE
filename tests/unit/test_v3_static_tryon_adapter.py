"""V3-016 — the `StaticTryOnPort` fake adapter, and the fixtures it runs on.

Container-free and network-free by construction, which is the point: contract
§6.4 restricts development and CI to fake/recorded adapters, so this adapter is
what every preview test in the suite will actually run. If it were
non-deterministic, V3-017 could not assert an exact stored object and V3-018's
quality verdict would be attached to an image a cache hit might not reproduce.

**Every fixture here is synthesised in-process** (the `_photo()` /
`_garment()` helpers, following `test_v3_avatar_preparation_adapter.py`). No
image bytes are committed to the repository and no real photograph is read, which
is the loop's safety rule — "Never send real user photos to a third-party
provider during development loops" — held one step earlier, at the point where
test material is produced at all. A rendered sample of the output is saved as
evidence under `.agent-artifacts/v3-016/`, derived entirely from these synthetic
inputs.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image, UnidentifiedImageError

from app.application.ports.static_tryon import (
    GarmentImage,
    GarmentSlot,
    PoseTemplate,
    StaticTryOnRequest,
    ViewRequirement,
)
from app.infrastructure.vision.fake_static_tryon import (
    MODEL_VERSION,
    PREVIEW_SIZE,
    PROMPT_VERSION,
    PROVIDER,
    FakeStaticTryOn,
)


def _photo(size: tuple[int, int], colour: tuple[int, int, int] = (200, 120, 60)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def _garment(slot: GarmentSlot, colour: tuple[int, int, int]) -> GarmentImage:
    buffer = io.BytesIO()
    Image.new("RGB", (400, 400), colour).save(buffer, format="PNG")
    return GarmentImage(image=buffer.getvalue(), slot=slot)


def _request(
    *,
    avatar: bytes | None = None,
    candidate: GarmentImage | None = None,
    supporting: tuple[GarmentImage, ...] = (),
) -> StaticTryOnRequest:
    return StaticTryOnRequest(
        avatar_image=avatar if avatar is not None else _photo((900, 1600)),
        candidate_garment=candidate
        if candidate is not None
        else _garment(GarmentSlot.TORSO, (20, 90, 160)),
        supporting_garments=supporting,
        pose_template=PoseTemplate.NEUTRAL_FULL_BODY,
        view=ViewRequirement.FRONT,
    )


async def test_render_returns_a_preview_on_the_canonical_canvas() -> None:
    result = await FakeStaticTryOn().render(_request())

    with Image.open(io.BytesIO(result.image)) as image:
        assert image.size == PREVIEW_SIZE
        assert image.format == "PNG"


async def test_render_is_deterministic() -> None:
    """Byte-identical output for identical input.

    What lets V3-017 assert an exact stored object rather than "some bytes", and
    what keeps the suite from flaking on a re-encode.
    """
    request = _request(supporting=(_garment(GarmentSlot.FEET, (240, 240, 230)),))
    port = FakeStaticTryOn()

    first = await port.render(request)
    second = await port.render(request)

    assert first.image == second.image


async def test_supporting_garment_order_does_not_change_the_render() -> None:
    """The property contract §6.3 requires, not a nicety.

    The strong cache key uses `sorted_supporting_item_ids`, so two requests that
    differ only in the order of the supporting garments are the *same* cached
    asset. A renderer whose output depended on that order would return an image
    a re-render could not reproduce, and V3-018's quality verdict would describe
    one arrangement while a later cache hit served another.
    """
    legs = _garment(GarmentSlot.LEGS, (40, 40, 60))
    feet = _garment(GarmentSlot.FEET, (240, 240, 230))
    avatar = _photo((900, 1600))
    candidate = _garment(GarmentSlot.TORSO, (20, 90, 160))
    port = FakeStaticTryOn()

    forwards = await port.render(
        _request(avatar=avatar, candidate=candidate, supporting=(legs, feet))
    )
    backwards = await port.render(
        _request(avatar=avatar, candidate=candidate, supporting=(feet, legs))
    )

    assert forwards.image == backwards.image


async def test_two_garments_in_one_slot_still_order_deterministically() -> None:
    """Slot order alone does not decide it, so the tie-break must be content.

    V3-006 can fill `torso` with a top while the candidate is also a top, so
    same-slot collisions are reachable rather than hypothetical. Without the
    digest tie-break the arrangement would depend on call order for exactly those
    outfits.
    """
    first_top = _garment(GarmentSlot.TORSO, (10, 10, 10))
    second_top = _garment(GarmentSlot.TORSO, (250, 250, 250))
    avatar = _photo((900, 1600))
    candidate = _garment(GarmentSlot.LEGS, (20, 90, 160))
    port = FakeStaticTryOn()

    forwards = await port.render(
        _request(avatar=avatar, candidate=candidate, supporting=(first_top, second_top))
    )
    backwards = await port.render(
        _request(avatar=avatar, candidate=candidate, supporting=(second_top, first_top))
    )

    assert forwards.image == backwards.image


async def test_the_candidate_garment_actually_reaches_the_render() -> None:
    """The one garment the decision is about cannot be silently dropped.

    Asserted by difference rather than by inspecting the layout: swapping only
    the candidate must change the bytes. A renderer that ignored the field would
    return the same image for both.
    """
    avatar = _photo((900, 1600))
    port = FakeStaticTryOn()

    blue = await port.render(
        _request(avatar=avatar, candidate=_garment(GarmentSlot.TORSO, (20, 90, 160)))
    )
    red = await port.render(
        _request(avatar=avatar, candidate=_garment(GarmentSlot.TORSO, (160, 20, 20)))
    )

    assert blue.image != red.image


async def test_a_supporting_garment_reaches_the_render_too() -> None:
    avatar = _photo((900, 1600))
    candidate = _garment(GarmentSlot.TORSO, (20, 90, 160))
    port = FakeStaticTryOn()

    alone = await port.render(_request(avatar=avatar, candidate=candidate))
    with_shoes = await port.render(
        _request(
            avatar=avatar,
            candidate=candidate,
            supporting=(_garment(GarmentSlot.FEET, (240, 240, 230)),),
        )
    )

    assert alone.image != with_shoes.image


async def test_geometry_is_normalized_regardless_of_input_aspect() -> None:
    """Every preview has the same geometry, whatever the avatar render was.

    Letterboxed rather than cropped: cropping a person to a fixed aspect is how a
    preview loses the feet of the outfit it exists to show.
    """
    port = FakeStaticTryOn()

    for size in ((900, 1600), (1600, 900), (1000, 1000)):
        result = await port.render(_request(avatar=_photo(size)))
        with Image.open(io.BytesIO(result.image)) as image:
            assert image.size == PREVIEW_SIZE


async def test_source_metadata_does_not_survive_into_the_preview() -> None:
    """No EXIF on a derived asset.

    A phone photo carries GPS and device identifiers. Those have no business on a
    generated preview that may be served by a signed URL, and contract §7 keeps
    identity material out of anything derived.
    """
    buffer = io.BytesIO()
    exif = Image.Exif()
    exif[0x010F] = "AcmePhone"
    Image.new("RGB", (900, 1600), (200, 120, 60)).save(buffer, format="JPEG", exif=exif)

    result = await FakeStaticTryOn().render(_request(avatar=buffer.getvalue()))

    with Image.open(io.BytesIO(result.image)) as image:
        assert not dict(image.getexif())


async def test_a_transparent_cutout_does_not_render_as_a_black_silhouette() -> None:
    """Wardrobe item images are cutouts, so alpha is the normal case here."""
    buffer = io.BytesIO()
    Image.new("RGBA", (400, 400), (0, 0, 0, 0)).save(buffer, format="PNG")
    transparent = GarmentImage(image=buffer.getvalue(), slot=GarmentSlot.TORSO)

    result = await FakeStaticTryOn().render(_request(candidate=transparent))

    with Image.open(io.BytesIO(result.image)) as image:
        assert image.mode == "RGB"
        # The garment tile sits in the top-right corner; a flattened transparent
        # cutout must show the panel colour there, not black.
        assert image.getpixel((PREVIEW_SIZE[0] - 5, 5)) != (0, 0, 0)


async def test_undecodable_bytes_raise_rather_than_returning_a_preview() -> None:
    """The port raises on failure; there is no partial-success return shape.

    A half-rendered preview is not something that may become `ready`, so the
    caller maps this to a failed generation instead of storing a placeholder.
    """
    with pytest.raises(UnidentifiedImageError):
        await FakeStaticTryOn().render(_request(avatar=b"not an image"))


async def test_the_result_names_the_pipeline_that_produced_it() -> None:
    """The anti-drift property that replaces V3-013's mirrored-constants test.

    V3-013 had to pin `preparation_pipeline_identity()` against
    `container.avatar_preparation()` because the identity was reconstructed in
    the application layer. Here the port reports its own identity and stamps the
    same value onto the result, so "what will run" and "what ran" are the same
    object and there is nothing to keep in sync.
    """
    port = FakeStaticTryOn()

    result = await port.render(_request())

    assert result.identity == port.pipeline_identity
    assert result.identity.provider == PROVIDER
    assert result.identity.model_version == MODEL_VERSION
    assert result.identity.prompt_version == PROMPT_VERSION


async def test_the_pipeline_identity_is_readable_without_rendering() -> None:
    """Contract §6.3 composes the cache key before any generation happens.

    A key that could only be computed from a result would never prevent a paid
    call, so this must not require `render` to have run.
    """
    identity = FakeStaticTryOn().pipeline_identity

    assert identity.as_slug() == f"{PROVIDER}:{MODEL_VERSION}:{PROMPT_VERSION}"
