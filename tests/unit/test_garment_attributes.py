from app.infrastructure.vision.qwen_vision import build_outfit_vision_prompt, build_vision_prompt, garment_attributes
from app.presentation.api.v1.schemas.wardrobe import season_tag_list


def test_formality_ladder_maps_to_the_item_scale() -> None:
    assert garment_attributes({"formality": 1})["formality_score"] == 10
    assert garment_attributes({"formality": 4})["formality_score"] == 70
    assert garment_attributes({"formality": 4.4})["formality_score"] == 70


def test_out_of_range_or_malformed_formality_is_unknown() -> None:
    for raw in (0, 6, "3", None, True, [3]):
        assert garment_attributes({"formality": raw})["formality_score"] is None


def test_seasons_keep_vocabulary_and_collapse_to_all_season() -> None:
    assert garment_attributes({"seasons": ["Summer", "spring", "monsoon"]})["season_tags"] == ["spring", "summer"]
    assert garment_attributes({"seasons": ["spring", "summer", "autumn", "winter"]})["season_tags"] == ["all_season"]
    assert garment_attributes({"seasons": ["all_season"]})["season_tags"] == ["all_season"]
    assert garment_attributes({"seasons": "summer"})["season_tags"] == []


def test_material_and_style_tags_are_bounded() -> None:
    attributes = garment_attributes(
        {"material": "Wool", "style_tags": ["Minimal", "minimal", "office!", "classic", "edgy", "street", "boho"]}
    )
    assert attributes["material"] == "wool"
    assert attributes["style_tags"] == ["minimal", "classic", "edgy", "street"]
    assert garment_attributes({"material": "unobtainium"})["material"] is None


def test_missing_attributes_are_unknown_not_defaults() -> None:
    assert garment_attributes({}) == {"formality_score": None, "season_tags": [], "material": None, "style_tags": []}


def test_prompts_ask_for_the_attributes() -> None:
    for prompt in (build_vision_prompt("zh-CN"), build_outfit_vision_prompt("zh-CN")):
        assert "formality" in prompt and "seasons" in prompt and "material" in prompt


def test_season_tag_list_unwraps_the_stored_shape() -> None:
    assert season_tag_list({"tags": ["spring", "summer"]}) == ["spring", "summer"]
    assert season_tag_list(None) is None
    assert season_tag_list(["summer"]) is None
