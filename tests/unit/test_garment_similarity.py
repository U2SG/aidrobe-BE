import io

from PIL import Image, ImageDraw

from app.infrastructure.vision.garment_similarity import prescreen


def _jpeg(color: tuple[int, int, int], size: tuple[int, int] = (120, 200), stripe: bool = False) -> bytes:
    image = Image.new("RGB", size, color)
    if stripe:
        for x in range(size[0]):  # dark-to-light gradient: a picture with structure
            ImageDraw.Draw(image).line((x, 0, x, size[1]), fill=(x * 2, 30, 30))
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG")
    return buffer.getvalue()


def test_clearly_different_colours_are_different_without_the_model() -> None:
    assert prescreen(_jpeg((200, 30, 30)), _jpeg((30, 60, 200)), same_photo=False) is False


def test_far_apart_proportions_are_different() -> None:
    assert prescreen(_jpeg((200, 30, 30), (60, 300)), _jpeg((200, 30, 30), (300, 60)), same_photo=False) is False


def test_similar_crops_from_different_photos_go_to_the_model() -> None:
    assert prescreen(_jpeg((200, 30, 30)), _jpeg((190, 35, 30)), same_photo=False) is None


def test_reupload_of_the_same_photo_is_same_without_the_model() -> None:
    crop = _jpeg((200, 30, 30), stripe=True)
    assert prescreen(crop, crop, same_photo=True) is True
    # Same photo but a clearly different picture: never decided locally.
    flipped = Image.open(io.BytesIO(crop)).transpose(Image.Transpose.FLIP_LEFT_RIGHT)
    buffer = io.BytesIO()
    flipped.save(buffer, format="JPEG")
    assert prescreen(crop, buffer.getvalue(), same_photo=True) is None


def test_unreadable_images_go_to_the_model() -> None:
    assert prescreen(b"not an image", b"also not", same_photo=False) is None
