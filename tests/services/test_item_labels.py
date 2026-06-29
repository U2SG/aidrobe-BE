from app.common.item_labels import localized_item_name


def test_localized_item_name_uses_locale_map_exact_then_language() -> None:
    assert (
        localized_item_name(
            title="Sneaker",
            color_main="white",
            category="shoes",
            locale="zh-CN",
            localized_names={"zh": "白色鞋款"},
            name_source="ai",
        )
        == "白色鞋款"
    )


def test_localized_item_name_existing_data_known_term_fallback() -> None:
    assert (
        localized_item_name(
            title="Sneaker",
            color_main="white",
            category="shoes",
            locale="zh-CN",
            localized_names=None,
            name_source="ai",
        )
        == "白色运动鞋"
    )
    assert (
        localized_item_name(
            title="Sneaker",
            color_main="white",
            category="shoes",
            locale="en-US",
            localized_names=None,
            name_source="ai",
        )
        == "White sneakers"
    )


def test_localized_item_name_unknown_color_falls_back_to_noun() -> None:
    # Color outside COLOR_LABELS (ivory/khaki) must not discard the localized
    # noun and leak the raw English title.
    assert (
        localized_item_name(
            title="Sneaker",
            color_main="ivory",
            category="shoes",
            locale="zh-CN",
            localized_names=None,
            name_source="ai",
        )
        == "运动鞋"
    )
    assert (
        localized_item_name(
            title="Pants",
            color_main="khaki",
            category="bottom",
            locale="en-US",
            localized_names=None,
            name_source="ai",
        )
        == "bottoms"
    )


def test_localized_item_name_user_title_wins_over_localized_names() -> None:
    assert (
        localized_item_name(
            title="My court shoes",
            color_main="white",
            category="shoes",
            locale="zh-CN",
            localized_names={"zh-CN": "白色运动鞋", "en-US": "White sneakers"},
            name_source="user",
        )
        == "My court shoes"
    )
