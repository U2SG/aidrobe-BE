"""BE#78 — context override → constraints, and their engine effects (pure)."""

from app.application.ports.llm import LlmAnswer
from app.application.recommendations.context_override import (
    COMFORT,
    EXCLUDE_DRESS,
    FORMAL,
    LOW_KEY,
    REDUCE_WEATHER,
    REMOVABLE_LAYER,
    constraints_from_chip,
    constraints_from_text,
    resolve_constraints,
)
from app.application.recommendations.engine import EngineItem, RecommendationEngine
from app.common.enums import Category


class _FakeLlm:
    def __init__(self, text: str) -> None:
        self._text = text

    async def polish_reason(self, prompt: str) -> str:  # pragma: no cover
        return ""

    async def ask(self, system_prompt: str, question: str, *, json_mode: bool = False) -> LlmAnswer:
        return LlmAnswer(text=self._text, tokens_used=1)


# --- chip mapping ------------------------------------------------------------

def test_chip_mapping_covers_all_fe_chips() -> None:
    assert constraints_from_chip("meeting") == [FORMAL]
    assert constraints_from_chip("comfy") == [COMFORT]
    assert constraints_from_chip("noSkirt") == [EXCLUDE_DRESS]
    assert constraints_from_chip("walk") == [COMFORT]
    assert constraints_from_chip("lowKey") == [LOW_KEY]
    assert constraints_from_chip("coldOffice") == [REMOVABLE_LAYER]
    assert constraints_from_chip("stayIn") == [REDUCE_WEATHER]
    assert constraints_from_chip("unknown_chip") == []
    assert constraints_from_chip(None) == []


# --- free-text LLM parse -----------------------------------------------------

async def test_free_text_parse_keeps_only_taxonomy_tokens() -> None:
    llm = _FakeLlm('{"constraints": ["formal", "make_me_rich", "exclude_dress"]}')
    out = await constraints_from_text("今天有重要会议还不想穿裙子", llm)
    assert out == [FORMAL, EXCLUDE_DRESS]  # bogus token dropped


async def test_free_text_degrades_without_llm() -> None:
    assert await constraints_from_text("随便", None) == []


async def test_free_text_degrades_on_empty_or_bad_json() -> None:
    assert await constraints_from_text("x", _FakeLlm("")) == []
    assert await constraints_from_text("x", _FakeLlm("not json")) == []
    assert await constraints_from_text("", _FakeLlm('{"constraints":["formal"]}')) == []


async def test_resolve_prefers_chip_and_skips_llm_for_chip_source() -> None:
    llm = _FakeLlm('{"constraints": ["comfort"]}')
    # chip is authoritative — LLM not consulted
    assert await resolve_constraints("meeting", "想舒服", "chip", llm) == [FORMAL]
    # explicit chip source with no known chip → no free-text fallback
    assert await resolve_constraints(None, "想舒服", "chip", llm) == []
    # free_text source → LLM path
    assert await resolve_constraints(None, "想舒服", "free_text", llm) == [COMFORT]


# --- engine effects ----------------------------------------------------------

def _item(cat: str, color: str = "black", item_id: str | None = None) -> EngineItem:
    return EngineItem(item_id=item_id or cat, title=cat, category=cat, color_main=color)


def test_score_removable_layer_and_formal_prefer_outerwear() -> None:
    engine = RecommendationEngine()
    with_outer = [_item(Category.TOP.value), _item(Category.OUTERWEAR.value)]
    without = [_item(Category.TOP.value), _item(Category.BOTTOM.value)]
    assert engine._context_constraint_score(with_outer, [REMOVABLE_LAYER], 0.8, 0.5) > 0
    assert engine._context_constraint_score(without, [REMOVABLE_LAYER], 0.8, 0.5) == 0.0
    assert engine._context_constraint_score(with_outer, [FORMAL], 0.8, 0.5) > 0


def test_score_low_key_penalizes_bright_colors() -> None:
    engine = RecommendationEngine()
    bright = [_item(Category.TOP.value, "red"), _item(Category.BOTTOM.value, "yellow")]
    neutral = [_item(Category.TOP.value, "black"), _item(Category.BOTTOM.value, "navy")]
    assert engine._context_constraint_score(bright, [LOW_KEY], 0.8, 0.5) < 0
    assert engine._context_constraint_score(neutral, [LOW_KEY], 0.8, 0.5) == 0.0


def test_score_empty_constraints_is_zero() -> None:
    engine = RecommendationEngine()
    combo = [_item(Category.TOP.value), _item(Category.OUTERWEAR.value)]
    assert engine._context_constraint_score(combo, [], 0.8, 0.5) == 0.0


async def test_exclude_dress_removes_dresses_from_generation() -> None:
    engine = RecommendationEngine()
    items = [
        _item(Category.TOP.value, item_id="top1"),
        _item(Category.BOTTOM.value, item_id="bottom1"),
        _item(Category.SHOES.value, item_id="shoes1"),
        _item(Category.DRESS.value, item_id="dress1"),
    ]
    result = await engine.generate(items, context_constraints=[EXCLUDE_DRESS])
    assert result.status == "ready"
    for outfit in result.outfits:
        assert "dress1" not in outfit.item_ids


async def test_exclude_dress_also_drops_skirt_bottoms() -> None:
    # Skirts are bottom items with sub_category == "skirt"; no-skirt must drop them.
    engine = RecommendationEngine()
    items = [
        _item(Category.TOP.value, item_id="top1"),
        EngineItem(
            item_id="skirt1",
            title="skirt",
            category=Category.BOTTOM.value,
            color_main="black",
            sub_category="skirt",
        ),
        _item(Category.BOTTOM.value, item_id="pants1"),
        _item(Category.SHOES.value, item_id="shoes1"),
    ]
    result = await engine.generate(items, context_constraints=[EXCLUDE_DRESS])
    assert result.status == "ready"
    for outfit in result.outfits:
        assert "skirt1" not in outfit.item_ids
