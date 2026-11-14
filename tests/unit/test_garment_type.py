import pytest

from app.application.purchase.garment_type import garment_type


@pytest.mark.parametrize(
    ("texts", "expected"),
    [
        (("graphic_tee",), "tee"),
        (("印花T恤",), "tee"),
        (("白色圆领 T 恤",), "tee"),
        (("黑色高领打底",), "turtleneck"),
        (("black turtleneck",), "turtleneck"),
        (("浅蓝牛津衬衫",), "shirt"),
        (("原色直筒牛仔裤",), "jeans"),
        (("卡其直筒裤",), "trousers"),
        (("藏青西装外套",), "blazer"),
        (("驼色羊毛大衣",), "coat"),
        (("浅绿色帆布鞋",), "sneakers"),
        (("棕色乐福鞋",), "loafers"),
        (("黑色连衣裙",), "dress"),
        ((None, "sneakers", "白色运动鞋"), "sneakers"),
    ],
)
def test_known_names(texts: tuple[str | None, ...], expected: str) -> None:
    assert garment_type(*texts) == expected


def test_unknown_or_empty_is_none() -> None:
    assert garment_type("藏青条纹衫") is None
    assert garment_type(None, "") is None
