"""Common-sense warmth rules on concrete garments (warmth_rules.py)."""

import pytest

from app.application.ports.weather import WeatherInfo
from app.application.recommendations.warmth_rules import (
    HEAVY_UPPER,
    HEAVY_UPPER_WITH_SHORT_BOTTOM,
    SHORT_BOTTOM,
    SUMMER_ONLY,
    SUMMER_ONLY_WHEN_COLD,
    WINTER_OUTER,
    WINTER_OUTER_WHEN_HOT,
    combo_warmth_violation,
    feels_like_c,
    garment_warmth_traits,
)


def _traits(category: str, *names: str, **attrs: object) -> frozenset[str]:
    return garment_warmth_traits(category=category, names=names, **attrs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("category", "name", "attrs", "expected"),
    [
        ("top", "Chunky knit sweater", {}, {HEAVY_UPPER}),
        ("top", "灰色高领毛衣", {}, {HEAVY_UPPER}),
        ("top", "Cream crewneck", {"material": "cashmere"}, {HEAVY_UPPER}),
        ("top", "Cream crewneck", {"material": "wool"}, {HEAVY_UPPER}),
        ("top", "Plain top", {"season_tags": ("winter",)}, {HEAVY_UPPER}),
        ("outerwear", "Black puffer jacket", {}, {HEAVY_UPPER, WINTER_OUTER}),
        ("outerwear", "黑色羽绒服", {}, {HEAVY_UPPER, WINTER_OUTER}),
        ("outerwear", "羽绒背心", {}, {HEAVY_UPPER, WINTER_OUTER}),  # down vest is not summer-only
        ("outerwear", "Camel wool coat", {}, {HEAVY_UPPER, WINTER_OUTER}),
        ("outerwear", "Navy jacket", {"material": "down"}, {HEAVY_UPPER, WINTER_OUTER}),
        ("bottom", "Denim shorts", {}, {SHORT_BOTTOM, SUMMER_ONLY}),
        ("bottom", "白色短裤", {}, {SHORT_BOTTOM, SUMMER_ONLY}),
        ("bottom", "Black skirt", {"sub_category": "mini_skirt"}, {SHORT_BOTTOM}),
        ("bottom", "Linen trousers", {"season_tags": ("summer",)}, {SUMMER_ONLY}),
        ("top", "Black tank top", {}, {SUMMER_ONLY}),
        ("top", "真丝吊带", {}, {SUMMER_ONLY}),
        ("top", "灰色针织背心", {}, set()),  # knit vest is a cold-weather layer
        ("outerwear", "Grey fleece vest", {}, set()),
        ("top", "黑色背心", {"material": "knit"}, set()),  # recognised knit material, generic name
        ("shoes", "Black sandals", {}, {SUMMER_ONLY}),
        ("shoes", "人字拖", {}, {SUMMER_ONLY}),
    ],
)
def test_garment_traits_from_names_material_and_tags(
    category: str, name: str, attrs: dict[str, object], expected: set[str]
) -> None:
    assert _traits(category, name, **attrs) == expected


@pytest.mark.parametrize(
    ("category", "name", "attrs"),
    [
        ("top", "White tee", {}),  # nothing known → no trait
        ("top", "Light cardigan", {}),  # cardigans layer over summer looks
        ("outerwear", "Navy wool blazer", {"material": "wool"}),  # wool alone is not heavy outerwear
        ("top", "Summer tee", {"season_tags": ("summer",)}),  # a summer tee layers fine in the cold
        ("top", "Tankini", {}),  # word boundary: not a "tank"
        ("bottom", "Midi skirt", {}),
        ("bottom", "Black trousers", {"season_tags": ("all_season",)}),
        ("shoes", "Leather boots", {}),
    ],
)
def test_unknown_or_ordinary_garments_have_no_traits(
    category: str, name: str, attrs: dict[str, object]
) -> None:
    assert _traits(category, name, **attrs) == frozenset()


def test_sub_category_counts_as_a_name() -> None:
    assert SHORT_BOTTOM in _traits("bottom", "Blue bottoms", sub_category="shorts")


SWEATER = _traits("top", "Chunky knit sweater")
DOWN_VEST = _traits("outerwear", "Down vest")
SHORTS = _traits("bottom", "Denim shorts")
JEANS = _traits("bottom", "Blue jeans")
TEE = _traits("top", "White tee")
SANDALS = _traits("shoes", "Black sandals")
PUFFER = _traits("outerwear", "Black puffer jacket")
DENIM_JACKET = _traits("outerwear", "Light denim jacket")


def test_heavy_upper_with_shorts_is_excluded_at_any_temperature_or_without_weather() -> None:
    assert combo_warmth_violation([SWEATER, SHORTS], None) == HEAVY_UPPER_WITH_SHORT_BOTTOM
    assert combo_warmth_violation([TEE, SHORTS, DOWN_VEST], 30.0) == HEAVY_UPPER_WITH_SHORT_BOTTOM
    assert combo_warmth_violation([SWEATER, JEANS], 5.0) is None


@pytest.mark.parametrize(("felt", "expected"), [(11.9, SUMMER_ONLY_WHEN_COLD), (12.0, None), (None, None)])
def test_summer_only_pieces_out_below_12c_feels_like(felt: float | None, expected: str | None) -> None:
    assert combo_warmth_violation([TEE, JEANS, SANDALS], felt) == expected


@pytest.mark.parametrize(("felt", "expected"), [(24.1, WINTER_OUTER_WHEN_HOT), (24.0, None), (None, None)])
def test_winter_outerwear_out_above_24c_feels_like(felt: float | None, expected: str | None) -> None:
    assert combo_warmth_violation([TEE, JEANS, PUFFER], felt) == expected


def test_light_layers_and_unknown_items_never_exclude() -> None:
    assert combo_warmth_violation([TEE, JEANS, DENIM_JACKET], 30.0) is None
    assert combo_warmth_violation([frozenset(), frozenset()], 0.0) is None


def test_feels_like_uses_provider_value_plus_thermal_offset() -> None:
    weather = WeatherInfo(temperature_c=14.0, feels_like_c=9.0, rain_probability=0.0, condition="windy")
    assert feels_like_c(weather) == 9.0
    assert feels_like_c(weather, thermal_offset_c=-3.0) == 6.0
    assert feels_like_c(None) is None


def test_feels_like_falls_back_to_air_temperature_when_missing() -> None:
    weather = WeatherInfo(temperature_c=14.0, feels_like_c=float("nan"), rain_probability=0.0, condition="x")
    assert feels_like_c(weather) == 14.0


def test_trace_replay_uses_recorded_warmth_traits() -> None:
    from evals.recommendations.replay import _engine_item

    recorded = _engine_item({"id": "a", "category": "top", "color": "grey", "warmth_traits": [HEAVY_UPPER]})
    legacy = _engine_item({"id": "b", "category": "top", "color": "grey"})
    assert recorded.warmth_traits == frozenset({HEAVY_UPPER})
    assert legacy.warmth_traits == frozenset()
