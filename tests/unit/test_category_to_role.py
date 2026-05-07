import pytest

from app.common.category_to_role import map_category_to_role


@pytest.mark.parametrize(
    ("category", "expected"),
    [
        ("top", "top"),
        ("shirt", "top"),
        ("dress", "top"),
        ("outerwear", "outer"),
        ("jacket", "outer"),
        ("pants", "bottom"),
        ("skirt", "bottom"),
        ("shoes", "shoes"),
        ("sneakers", "shoes"),
        ("bag", "accessory"),
        ("accessory", "accessory"),
        ("Top", "top"),
        ("  pants  ", "bottom"),
    ],
)
def test_known_categories(category: str, expected: str) -> None:
    assert map_category_to_role(category) == expected


@pytest.mark.parametrize("category", [None, "", "weirdthing", "magic-item"])
def test_unknown_falls_back_to_accessory(category: str | None) -> None:
    assert map_category_to_role(category) == "accessory"
