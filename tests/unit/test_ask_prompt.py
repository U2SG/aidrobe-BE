"""Pure-function coverage for the Ask system-prompt builder (no DB/Docker)."""

from __future__ import annotations

import uuid

from app.application.ask.answer_question import _build_system_prompt, _preamble_for
from app.infrastructure.persistence.models.wardrobe import WardrobeItem


def _items() -> list[WardrobeItem]:
    return [
        WardrobeItem(
            user_id=uuid.uuid4(),
            title="White shirt",
            category="top",
            color_main="white",
            status="wearable",
            season_tags=["summer"],
            location="home",
        ),
        WardrobeItem(
            user_id=uuid.uuid4(),
            title="Black pants",
            category="bottom",
            color_main="black",
            status="wearable",
        ),
    ]


def test_preamble_defaults_to_chinese() -> None:
    assert _preamble_for(None).startswith("你是 Aidrobe")
    assert _preamble_for("").startswith("你是 Aidrobe")
    assert _preamble_for("fr").startswith("你是 Aidrobe")  # unknown -> default


def test_preamble_matches_language_subtag() -> None:
    assert _preamble_for("en").startswith("You are Aidrobe")
    assert _preamble_for("en-US").startswith("You are Aidrobe")
    assert _preamble_for("EN").startswith("You are Aidrobe")
    assert _preamble_for("zh-CN").startswith("你是 Aidrobe")


def test_build_prompt_feeds_wardrobe_and_hides_titles() -> None:
    prompt = _build_system_prompt(uuid.uuid4(), _items(), None)
    # Sanitized structured attributes reach the prompt...
    assert '"category": "top"' in prompt
    assert '"color_main": "white"' in prompt
    assert '"category": "bottom"' in prompt
    # ...but free-form titles never do, and the opaque token is attached.
    assert "White shirt" not in prompt
    assert "usr_" in prompt


def test_build_prompt_honors_locale() -> None:
    assert _build_system_prompt(uuid.uuid4(), [], "en").startswith("You are Aidrobe")
    assert _build_system_prompt(uuid.uuid4(), [], "zh-CN").startswith("你是 Aidrobe")
