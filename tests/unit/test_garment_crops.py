import io

from PIL import Image

from app.infrastructure.vision.qwen_vision import garment_box
from app.infrastructure.workers.garment_crops import crop_regions


def _two_tone(width: int = 200, height: int = 400) -> bytes:
    image = Image.new("RGB", (width, height), (220, 20, 20))  # red top half
    image.paste((20, 20, 220), (0, height // 2, width, height))  # blue bottom half
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _centre_colour(jpeg: bytes) -> tuple[int, int, int]:
    with Image.open(io.BytesIO(jpeg)) as image:
        rgb = image.convert("RGB")
        return rgb.getpixel((rgb.width // 2, rgb.height // 2))  # type: ignore[return-value]


def test_crops_frame_the_boxed_region_without_recolouring() -> None:
    top, bottom, none = crop_regions(_two_tone(), [(0.1, 0.05, 0.9, 0.45), (0.1, 0.55, 0.9, 0.95), None])

    assert top is not None and bottom is not None and none is None
    r, g, b = _centre_colour(top)
    assert r > 180 and g < 60 and b < 60  # still the source red
    r, g, b = _centre_colour(bottom)
    assert b > 180 and r < 60 and g < 60  # still the source blue
    with Image.open(io.BytesIO(top)) as image:
        # 0.8 x 0.4 of 200 x 400, plus 6% padding each side.
        assert 170 <= image.width <= 200 and 160 <= image.height <= 190


def test_garment_box_rejects_unusable_boxes() -> None:
    assert garment_box([0.1, 0.2, 0.6, 0.9]) == (0.1, 0.2, 0.6, 0.9)
    assert garment_box([-0.2, 0.0, 1.4, 0.5]) == (0.0, 0.0, 1.0, 0.5)  # clamped
    for raw in (None, [0.1, 0.2, 0.6], [0.5, 0.5, 0.52, 0.9], [0.6, 0.2, 0.1, 0.9], ["0", 0, 1, 1], [True, 0, 1, 1]):
        assert garment_box(raw) is None
