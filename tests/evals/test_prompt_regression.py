"""Prompt-construction regression scenario for the composable builder (BE#66).

Unlike the baked-output fixtures in this directory (which check a static
recommendation `output`), this exercises the NEW prompt builder itself
(`_build_system_prompt` composing the `llm_prompts` modules) and locks the
grounding/preference/no-leak invariants the issue's acceptance criteria require.
"""

from __future__ import annotations

import uuid
from datetime import date

from app.application.ask.answer_question import _build_system_prompt
from app.application.ask.wardrobe_context import WardrobeContext, summarize_wardrobe
from app.infrastructure.persistence.models.wardrobe import WardrobeItem

_TODAY = date(2026, 6, 29)


def _ctx(items: list[WardrobeItem], *, locale: str = "zh") -> WardrobeContext:
    return WardrobeContext(
        scope="wardrobe",
        focused_item_id=None,
        locale=locale,
        wardrobe_summary=summarize_wardrobe(items),
        style_profile={"style_tags": [], "preferred_colors": []},
        fit_preferences={},
        recent_categories=[],
        departure_time=None,
        candidate_item_ids=[str(i.id) for i in items],
        candidate_selection={},
        preference_memory={
            "negative": [{"type": "fit", "value": "oversized"}],
            "positive": [{"type": "color", "value": "navy"}],
        },
    )


def _items() -> list[WardrobeItem]:
    return [
        WardrobeItem(
            user_id=uuid.uuid4(),
            title="Secret brand oversized hoodie",  # raw title must NOT leak
            category="top",
            color_main="navy",
            status="wearable",
        )
    ]


def test_builder_grounds_preferences_weather_and_hides_titles() -> None:
    items = _items()
    prompt = _build_system_prompt(uuid.uuid4(), items, _ctx(items), _TODAY, structured=True)

    # Preference memory is rendered as a PRIORITIZED hard avoid-rule, not just
    # buried in JSON (the issue's "included but not prioritized" regression).
    assert "必须避免" in prompt
    assert "oversized" in prompt
    # Reasoning policy makes weather/occasion decision dimensions explicit.
    assert "天气" in prompt and "场合" in prompt
    # Structured contract is centralized and present when requested.
    assert "referenced_items" in prompt
    # Grounding/privacy: structured attributes reach the prompt, raw titles never.
    assert '"color_main": "navy"' in prompt
    assert "Secret brand oversized hoodie" not in prompt
    # Persona leads so the response-language/voice behavior is stable.
    assert prompt.startswith("你是 Aidrobe")
