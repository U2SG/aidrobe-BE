from app.application.ports.weather import WeatherInfo
from app.application.recommendations.engine import EngineItem, RecommendationEngine


def test_fewer_than_three_items_returns_insufficient_items() -> None:
    result = RecommendationEngine().generate(
        [
            EngineItem("1", "White shirt", "top", "white"),
            EngineItem("2", "Black pants", "bottom", "black"),
        ]
    )

    assert result.status == "insufficient_items"
    assert result.guidance_text is not None


def test_three_items_can_produce_at_least_one_outfit() -> None:
    result = RecommendationEngine().generate(
        [
            EngineItem("1", "White shirt", "top", "white"),
            EngineItem("2", "Blue shirt", "top", "blue"),
            EngineItem("3", "Black pants", "bottom", "black"),
        ]
    )

    assert result.status == "ready"
    assert len(result.outfits) >= 1
    assert {"1", "3"} in [set(outfit.item_ids) for outfit in result.outfits]


def test_three_final_outfits_do_not_repeat_main_item_when_possible() -> None:
    result = RecommendationEngine().generate(
        [
            EngineItem("1", "White shirt", "top", "white"),
            EngineItem("2", "Blue shirt", "top", "blue"),
            EngineItem("3", "Gray shirt", "top", "gray"),
            EngineItem("4", "Black pants", "bottom", "black"),
        ]
    )

    main_items = [outfit.item_ids[0] for outfit in result.outfits]
    assert len(main_items) == len(set(main_items))


def test_recent_worn_main_item_is_downgraded() -> None:
    result = RecommendationEngine().generate(
        [
            EngineItem("1", "White shirt", "top", "white"),
            EngineItem("2", "Blue shirt", "top", "blue"),
            EngineItem("3", "Black pants", "bottom", "black"),
        ],
        recent_worn_main_item_ids={"1"},
    )

    top_outfit = max(result.outfits, key=lambda outfit: outfit.score_total)
    assert top_outfit.item_ids[0] != "1"


def test_reasons_are_template_generated_and_include_subscores() -> None:
    result = RecommendationEngine().generate(
        [
            EngineItem("1", "White shirt", "top", "white"),
            EngineItem("2", "Blue shirt", "top", "blue"),
            EngineItem("3", "Black pants", "bottom", "black"),
        ]
    )

    assert all(outfit.reason_text for outfit in result.outfits)
    assert all("color_score" in outfit.score_breakdown for outfit in result.outfits)


def test_no_longer_fits_items_are_hard_filtered() -> None:
    result = RecommendationEngine().generate(
        [
            EngineItem("1", "Old white shirt", "top", "white", fit_status="no_longer_fits"),
            EngineItem("2", "Blue shirt", "top", "blue"),
            EngineItem("3", "Black pants", "bottom", "black"),
            EngineItem("4", "Black shoes", "shoes", "black"),
        ]
    )

    assert result.status == "ready"
    assert all("1" not in outfit.item_ids for outfit in result.outfits)


def test_repetition_penalty_downgrades_recently_worn_similar_outfit() -> None:
    items = [
        EngineItem("1", "White shirt", "top", "white"),
        EngineItem("2", "Black pants", "bottom", "black"),
        EngineItem("3", "Black shoes", "shoes", "black"),
    ]
    repeated = RecommendationEngine().generate(
        items,
        recent_worn_outfit_item_ids=[{"1", "2", "3"}],
    )
    fresh = RecommendationEngine().generate(items)

    assert repeated.outfits[0].score_breakdown["repetition_penalty"] > 0
    assert repeated.outfits[0].score_total < fresh.outfits[0].score_total


def test_weather_score_falls_back_to_neutral_without_weather_data() -> None:
    result = RecommendationEngine().generate(
        [
            EngineItem("1", "White shirt", "top", "white"),
            EngineItem("2", "Black pants", "bottom", "black"),
            EngineItem("3", "Black shoes", "shoes", "black"),
        ],
        style_profile={"color_weights": {"white": 1.0}},
    )

    assert result.outfits[0].score_breakdown["weather_score"] == 0.5


def test_weather_penalizes_light_shoes_on_rainy_days() -> None:
    result = RecommendationEngine().generate(
        [
            EngineItem("1", "White shirt", "top", "white"),
            EngineItem("2", "Black pants", "bottom", "black"),
            EngineItem("3", "White shoes", "shoes", "white"),
        ],
        weather=WeatherInfo(
            temperature_c=18.0,
            feels_like_c=18.0,
            rain_probability=0.8,
            condition="Rain",
        ),
        style_profile={"color_weights": {"white": 1.0}},
    )

    assert result.outfits[0].score_breakdown["weather_score"] < 0.8
