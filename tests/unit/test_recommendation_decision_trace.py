import json
import uuid
from datetime import UTC, date, datetime
from decimal import Decimal

from app.application.ports.weather import WeatherInfo
from app.application.recommendations.decision_trace import (
    TRACE_SCHEMA_VERSION,
    build_decision_trace_payload,
)
from app.application.recommendations.engine import EngineOutfit, EngineResult
from app.infrastructure.persistence.models.preference_memory import UserPreferenceMemory
from app.infrastructure.persistence.models.wardrobe import WardrobeItem


def test_trace_payload_is_versioned_replayable_and_privacy_safe() -> None:
    item_id = uuid.uuid4()
    raw_override = "wear this with my private free-text note"
    image_url = "https://private.example/signed-item.jpg"
    memory_text = "raw preference sentence that must not be persisted"
    item = WardrobeItem(
        id=item_id,
        user_id=uuid.uuid4(),
        title=raw_override,
        category="top",
        sub_category="shirt",
        color_main="Blue",
        color_secondary="White",
        style_tags={"tags": ["minimal"], "image_url": image_url},
        season_tags={"tags": ["spring"]},
        fit_status="fits_well",
        fit_intent="regular",
        status="wearable",
        location="private closet name",
        wear_count=3,
        last_worn_at=date(2026, 8, 1),
        cover_image_url=image_url,
        original_image_url=image_url,
    )
    memory = UserPreferenceMemory(
        id=uuid.uuid4(),
        user_id=item.user_id,
        preference_type="color",
        preference_value=memory_text,
        polarity="negative",
        confidence=Decimal("0.80"),
        source="explicit_user_statement",
    )
    candidate = EngineOutfit(
        outfit_type="safe",
        title="safe",
        reason_text="not persisted in trace",
        item_ids=[str(item_id)],
        score_total=0.75,
        score_breakdown={"color_score": 0.75},
    )
    rejected = EngineOutfit(
        outfit_type="stylish",
        title="stylish",
        reason_text="also not persisted in trace",
        item_ids=[str(item_id)],
        score_total=0.5,
        score_breakdown={"color_score": 0.5},
    )
    result = EngineResult(
        status="ready",
        outfits=[candidate],
        candidates=[candidate, rejected],
    )

    payload = build_decision_trace_payload(
        target_date=date(2026, 8, 5),
        captured_at=datetime(2026, 8, 5, 1, 2, tzinfo=UTC),
        timezone_bucket="Asia/Singapore",
        weather=WeatherInfo(
            temperature_c=30,
            feels_like_c=32,
            rain_probability=0.2,
            condition="cloudy",
        ),
        weather_resolved_at="2026-08-05T01:01:00+00:00",
        commute_exposure="less_outdoor",
        indoor_climate=None,
        normalized_constraints=["formal", raw_override],
        wardrobe_items=[item],
        memory_items=[memory],
        engine_result=result,
        recent_worn_main_item_ids=set(),
        recent_outfit_item_ids=[],
        style_profile={"color_weights": {"blue": 1.0}},
        fit_profile={"category_preferences": {"top": "prefers_fitted"}},
        disliked_colors={"red"},
        relationship_edges=[],
        suppressed_outfit_item_ids=[],
        avoid_exact_repeats=True,
        persisted_rules_version="rules-v1",
        llm_provider="openai",
        llm_model="model-v1",
        deterministic_seed=17,
    )

    assert payload["schema_version"] == TRACE_SCHEMA_VERSION
    assert payload["context"]["normalized_constraints"] == ["formal"]
    assert payload["context"]["weather"]["provider_or_cache_resolved_at"] == (
        "2026-08-05T01:01:00+00:00"
    )
    assert payload["wardrobe"]["items"][0]["location_bucket"] == "location_1"
    assert payload["memory"]["items"][0] == {
        "id": str(memory.id),
        "type": "color",
        "confidence": 0.8,
        "source": "explicit_user_statement",
    }
    assert payload["candidate_trace"]["candidates"][0]["selected_rank"] == 1
    assert payload["candidate_trace"]["candidates"][0]["score_breakdown"] == {
        "color_score": 0.75
    }
    assert payload["candidate_trace"]["candidates"][1][
        "filter_rejection_reasons"
    ] == ["not_selected"]
    assert payload["provenance"]["engine_version"]
    assert payload["provenance"]["rules_version"] == "rules-v1"
    assert payload["provenance"]["ranker_schema_version"]
    assert payload["provenance"]["prompt_schema_version"]
    assert payload["provenance"]["llm"] == {
        "provider": "openai",
        "model": "model-v1",
    }
    assert payload["provenance"]["deterministic_seed"] == 17
    assert payload["provenance"]["reason_output_schema_version"]

    serialized = json.dumps(payload, ensure_ascii=False)
    assert raw_override not in serialized
    assert memory_text not in serialized
    assert image_url not in serialized
    assert "image_url" not in serialized
    assert "not persisted in trace" not in serialized
    assert "private closet name" not in serialized
