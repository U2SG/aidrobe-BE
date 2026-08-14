"""V3-013 — the `AvatarPreparationPort` fake adapter.

Container-free and network-free by construction, which is the point: contract
§6.4 restricts development and CI to fake/recorded adapters, so this adapter is
what every preparation test in the suite actually runs. If it were
non-deterministic, the worker tests could not assert a real `preparing → ready`
transition; if it leaked EXIF, a derived asset would carry the GPS coordinates of
where a user photographed themselves.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image, UnidentifiedImageError

from app.infrastructure.vision.fake_avatar_preparation import (
    CANONICAL_SIZE,
    MODEL_VERSION,
    PROMPT_VERSION,
    PROVIDER,
    FakeAvatarPreparation,
)


def _photo(size: tuple[int, int], colour: tuple[int, int, int] = (200, 120, 60)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, colour).save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


async def test_prepare_returns_a_canonical_front_render() -> None:
    prepared = await FakeAvatarPreparation().prepare(source_image=_photo((900, 1600)))

    with Image.open(io.BytesIO(prepared.front_image)) as image:
        assert image.size == CANONICAL_SIZE
        assert image.format == "PNG"


async def test_prepare_is_deterministic() -> None:
    """Byte-identical output for identical input.

    What lets a worker test assert an exact stored object rather than "some
    bytes", and what keeps the suite from flaking on a re-encode.
    """
    source = _photo((640, 960))
    port = FakeAvatarPreparation()

    first = await port.prepare(source_image=source)
    second = await port.prepare(source_image=source)

    assert first.front_image == second.front_image


async def test_geometry_is_normalized_regardless_of_input_aspect(
) -> None:
    """Every canonical render has the same geometry, whatever the phone produced.

    Letterboxed rather than cropped or stretched: cropping to a fixed aspect can
    cut off the head or the hem — the very framing V3-012's rules check for — and
    stretching distorts the person's proportions, which is a body claim made by
    accident.
    """
    port = FakeAvatarPreparation()

    for size in ((1600, 900), (900, 1600), (1000, 1000)):
        prepared = await port.prepare(source_image=_photo(size))
        with Image.open(io.BytesIO(prepared.front_image)) as image:
            assert image.size == CANONICAL_SIZE


async def test_exif_is_not_carried_into_the_derived_asset() -> None:
    """A derived asset must not inherit the source photo's metadata.

    Phone EXIF routinely carries GPS coordinates and device identifiers. Keeping
    them on a generated avatar render would persist location data about a person
    into a second object, for no purpose the feature has — minimum-necessary data
    (V3_SPEC §15).
    """
    buffer = io.BytesIO()
    source = Image.new("RGB", (800, 1200), (10, 20, 30))
    exif = source.getexif()
    # 0x0110 = Model, 0x8825 = GPSInfo pointer. Enough to prove the transform
    # does not copy the block through.
    exif[0x0110] = "SecretPhone9"
    source.save(buffer, format="JPEG", exif=exif)

    prepared = await FakeAvatarPreparation().prepare(source_image=buffer.getvalue())

    with Image.open(io.BytesIO(prepared.front_image)) as image:
        assert not dict(image.getexif())
    assert b"SecretPhone9" not in prepared.front_image


async def test_provenance_is_reported_for_the_version_row() -> None:
    """`provider` / `model_version` / `prompt_version` are what the row records.

    Internal columns (contract §6.4) — a separate test asserts they never reach a
    response — but they must be *present*, because the preparation idempotency key
    is derived from them and V3-016's acceptance is that assets record them.
    """
    prepared = await FakeAvatarPreparation().prepare(source_image=_photo((600, 800)))

    assert prepared.provider == PROVIDER
    assert prepared.model_version == MODEL_VERSION
    assert prepared.prompt_version == PROMPT_VERSION


async def test_no_side_view_is_synthesized() -> None:
    """One consented photo is one view (V3_SPEC §7.4/§7.5).

    Inventing a second angle from a single frame is fabrication, and a side view
    is explicitly a later opt-in enhancement rather than a P0 requirement.
    """
    prepared = await FakeAvatarPreparation().prepare(source_image=_photo((600, 800)))

    assert prepared.side_image is None


async def test_prepared_assets_carry_no_body_fields() -> None:
    """Acceptance 4 at the port boundary.

    The result type has nowhere to put a measurement, so an adapter cannot return
    one without editing the port — a review event rather than a silent addition.
    """
    prepared = await FakeAvatarPreparation().prepare(source_image=_photo((600, 800)))

    assert set(vars(prepared)) == {
        "front_image",
        "side_image",
        "provider",
        "model_version",
        "prompt_version",
    }


async def test_undecodable_bytes_raise() -> None:
    """Failure is an exception, not a half-prepared avatar.

    The worker maps this to a failed preparation, which leaves the previous
    version active. A port that returned something usable-looking for garbage
    input would let a broken render reach `ready`.

    `UnidentifiedImageError` specifically, not a blind `Exception`: the worker
    persists only the exception *type*, so which type this is becomes the
    operator-visible signal for "the stored object was not an image".
    """
    with pytest.raises(UnidentifiedImageError):
        await FakeAvatarPreparation().prepare(source_image=b"not an image")
