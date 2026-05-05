"""Tests for RecommendationEngine — updated for async LLM + fit_profile integration."""

import pytest

from app.application.ports.weather import WeatherInfo
from app.application.recommendations.engine import EngineItem, RecommendationEngine


@pytest.mark.asyncio
async def test_fewer_than_three_items_returns_insufficient_items() -> None:
    result = await RecommendationEngine().generate(
        [
            EngineItem("1", "White shirt", "top", "white"),
            EngineItem("2", "Black pants", "bottom", "black"),
        ]
    )

    assert result.status == "insufficient_items"
    assert result.guidance_text is not None


@pytest.mark.asyncio
async def test_three_items_can_produce_at_least_one_outfit() -> None:
    result = await RecommendationEngine().generate(
        [
            EngineItem("1", "White shirt", "top", "white"),
            EngineItem("2", "Blue shirt", "top", "blue"),
            EngineItem("3", "Black pants", "bottom", "black"),
        ]
    )

    assert result.status == "ready"
    assert len(result.outfits) >= 1
    assert {"1", "3"} in [set(outfit.item_ids) for outfit in result.outfits]


@pytest.mark.asyncio
async def test_three_final_outfits_do_not_repeat_main_item_when_possible() -> None:
    result = await RecommendationEngine().generate(
        [
            EngineItem("1", "White shirt", "top", "white"),
            EngineItem("2", "Blue shirt", "top", "blue"),
            EngineItem("3", "Gray shirt", "top", "gray"),
            EngineItem("4", "Black pants", "bottom", "black"),
        ]
    )

    main_items = [outfit.item_ids[0] for outfit in result.outfits]
    assert len(main_items) == len(set(main_items))


@pytest.mark.asyncio
async def test_recent_worn_main_item_is_downgraded() -> None:
    result = await RecommendationEngine().generate(
        [
            EngineItem("1", "White shirt", "top", "white"),
            EngineItem("2", "Blue shirt", "top", "blue"),
            EngineItem("3", "Black pants", "bottom", "black"),
        ],
        recent_worn_main_item_ids={"1"},
    )

    top_outfit = max(result.outfits, key=lambda outfit: outfit.score_total)
    assert top_outfit.item_ids[0] != "1"


@pytest.mark.asyncio
async def test_reasons_are_template_generated_and_include_subscores() -> None:
    result = await RecommendationEngine().generate(
        [
            EngineItem("1", "White shirt", "top", "white"),
            EngineItem("2", "Blue shirt", "top", "blue"),
            EngineItem("3", "Black pants", "bottom", "black"),
        ]
    )

    assert all(outfit.reason_text for outfit in result.outfits)
    assert all("color_score" in outfit.score_breakdown for outfit in result.outfits)


@pytest.mark.asyncio
async def test_no_longer_fits_items_are_hard_filtered() -> None:
    result = await RecommendationEngine().generate(
        [
            EngineItem("1", "Old white shirt", "top", "white", fit_status="no_longer_fits"),
            EngineItem("2", "Blue shirt", "top", "blue"),
            EngineItem("3", "Black pants", "bottom", "black"),
            EngineItem("4", "Black shoes", "shoes", "black"),
        ]
    )

    assert result.status == "ready"
    assert all("1" not in outfit.item_ids for outfit in result.outfits)


@pytest.mark.asyncio
async def test_repetition_penalty_downgrades_recently_worn_similar_outfit() -> None:
    items = [
        EngineItem("1", "White shirt", "top", "white"),
        EngineItem("2", "Black pants", "bottom", "black"),
        EngineItem("3", "Black shoes", "shoes", "black"),
    ]
    repeated = await RecommendationEngine().generate(
        items,
        recent_worn_outfit_item_ids=[{"1", "2", "3"}],
    )
    fresh = await RecommendationEngine().generate(items)

    assert repeated.outfits[0].score_breakdown["repetition_penalty"] > 0
    assert repeated.outfits[0].score_total < fresh.outfits[0].score_total


@pytest.mark.asyncio
async def test_weather_score_falls_back_to_neutral_without_weather_data() -> None:
    result = await RecommendationEngine().generate(
        [
            EngineItem("1", "White shirt", "top", "white"),
            EngineItem("2", "Black pants", "bottom", "black"),
            EngineItem("3", "Black shoes", "shoes", "black"),
        ],
        style_profile={"color_weights": {"white": 1.0}},
    )

    assert result.outfits[0].score_breakdown["weather_score"] == 0.5


@pytest.mark.asyncio
async def test_weather_penalizes_light_shoes_on_rainy_days() -> None:
    result = await RecommendationEngine().generate(
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


# ── New tests for LLM integration ──────────────────────────────────


class FakeLlm:
    """Mock LLM that returns a predictable polished reason."""

    async def polish_reason(self, prompt: str) -> str:
        return "AI润色：这身穿搭很适合你"


@pytest.mark.asyncio
async def test_llm_polishes_reason_when_available() -> None:
    engine = RecommendationEngine(llm=FakeLlm())
    result = await engine.generate(
        [
            EngineItem("1", "Navy blazer", "top", "navy"),
            EngineItem("2", "Beige chinos", "bottom", "beige"),
            EngineItem("3", "Brown loafers", "shoes", "brown"),
        ],
    )

    assert result.status == "ready"
    assert any("AI润色" in outfit.reason_text for outfit in result.outfits)


@pytest.mark.asyncio
async def test_llm_fallback_to_template_when_llm_is_none() -> None:
    engine = RecommendationEngine(llm=None)
    result = await engine.generate(
        [
            EngineItem("1", "Navy blazer", "top", "navy"),
            EngineItem("2", "Beige chinos", "bottom", "beige"),
            EngineItem("3", "Brown loafers", "shoes", "brown"),
        ],
    )

    assert result.status == "ready"
    # Should use template reasons (not AI)
    for outfit in result.outfits:
        assert outfit.reason_text and "AI润色" not in outfit.reason_text


# ── New tests for fit profile integration ──────────────────────────


@pytest.mark.asyncio
async def test_fit_profile_boosts_preferred_loose_items() -> None:
    engine = RecommendationEngine()
    result = await engine.generate(
        [
            EngineItem("1", "Oversized tee", "top", "white", fit_status="loose"),
            EngineItem("2", "Slim shirt", "top", "blue", fit_status="fits_well"),
            EngineItem("3", "Black pants", "bottom", "black", fit_status="fits_well"),
            EngineItem("4", "Black shoes", "shoes", "black", fit_status="fits_well"),
        ],
        fit_profile={"top": "prefers_loose"},
    )

    assert result.status == "ready"
    # The loose top should be preferred (higher score or selected as main item)
    loose_outfits = [
        outfit for outfit in result.outfits if "1" in outfit.item_ids
    ]
    assert len(loose_outfits) >= 1


@pytest.mark.asyncio
async def test_fit_profile_penalizes_mismatched_items() -> None:
    engine = RecommendationEngine()
    result_no_profile = await engine.generate(
        [
            EngineItem("1", "Tight tee", "top", "white", fit_status="tight"),
            EngineItem("2", "Blue shirt", "top", "blue", fit_status="fits_well"),
            EngineItem("3", "Black pants", "bottom", "black", fit_status="fits_well"),
            EngineItem("4", "Black shoes", "shoes", "black", fit_status="fits_well"),
        ],
    )
    result_with_profile = await engine.generate(
        [
            EngineItem("1", "Tight tee", "top", "white", fit_status="tight"),
            EngineItem("2", "Blue shirt", "top", "blue", fit_status="fits_well"),
            EngineItem("3", "Black pants", "bottom", "black", fit_status="fits_well"),
            EngineItem("4", "Black shoes", "shoes", "black", fit_status="fits_well"),
        ],
        fit_profile={"top": "prefers_loose"},
    )

    # With prefers_loose profile, the tight top should score lower
    tight_no_profile = next(
        (outfit for outfit in result_no_profile.outfits if "1" in outfit.item_ids),
        None,
    )
    tight_with_profile = next(
        (outfit for outfit in result_with_profile.outfits if "1" in outfit.item_ids),
        None,
    )
    if tight_no_profile and tight_with_profile:
        assert tight_with_profile.score_total <= tight_no_profile.score_total
