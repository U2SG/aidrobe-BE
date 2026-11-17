"""Conditions alter real garment selection; intent recognition stays conservative."""

import uuid

import pytest

from app.application.ask.purchase_context import explicit_purchase_choice, has_independent_preference_evidence
from app.application.purchase.candidate_normalizer import ParsedItem
from app.application.purchase.outfit_composition import CompositionConstraints, build_outfit_evidence
from app.application.purchase.wardrobe_similarity import OwnedItemFacts


def _item(category: str, *, tags: tuple[str, ...] = ()) -> OwnedItemFacts:
    return OwnedItemFacts(item_id=uuid.uuid4(), category=category, color_main="black", style_tags=tags)


def test_scene_and_style_change_the_actual_first_outfit() -> None:
    plain = _item("top")
    scene = _item("top", tags=("formal",))
    style = _item("top", tags=("streetwear",))
    bottom, shoes = _item("bottom"), _item("shoes")
    candidate = ParsedItem(category="outerwear", colors=["gray"], scene_tags=["casual"])
    pool = [plain, scene, style, bottom, shoes]
    legacy = build_outfit_evidence(candidate, pool)
    formal = build_outfit_evidence(candidate, pool, constraints=CompositionConstraints(scene="formal"))
    street = build_outfit_evidence(candidate, pool, constraints=CompositionConstraints(style_tags=("streetwear",)))
    assert plain.item_id in legacy.outfits[0].item_ids
    assert scene.item_id in formal.outfits[0].item_ids
    assert formal.outfits[0].scene == "formal" and formal.outfits[0].scene_source == "item_tags"
    assert style.item_id in street.outfits[0].item_ids


def test_every_outfit_keeps_locks_and_excludes_rejected_pieces() -> None:
    top, other_top, bottom, shoes = _item("top"), _item("top"), _item("bottom"), _item("shoes")
    evidence = build_outfit_evidence(
        ParsedItem(category="outerwear", colors=["gray"]), [other_top, top, bottom, shoes],
        constraints=CompositionConstraints(locked_item_ids=(top.item_id,), excluded_item_ids=(other_top.item_id,)),
    )
    assert evidence.outfits
    assert all(top.item_id in outfit.item_ids and other_top.item_id not in outfit.item_ids
               for outfit in evidence.outfits)


def test_candidate_cannot_silently_replace_a_locked_piece() -> None:
    top, bottom, shoes = _item("top"), _item("bottom"), _item("shoes")
    evidence = build_outfit_evidence(
        ParsedItem(category="top", colors=["gray"]), [top, bottom, shoes], similar_item_ids=(top.item_id,),
        constraints=CompositionConstraints(locked_item_ids=(top.item_id,)),
    )
    assert not evidence.outfits
    assert not evidence.missing_categories  # A collision is not an invitation to buy more clothes.
    assert evidence.alternatives and top.item_id in evidence.alternatives[0].item_ids


@pytest.mark.parametrize(("question", "choice"), [
    ("那先不买了，继续用已有衣服搭", "skip"), ("我打算买这件", "buy"), ("再想想吧", "later"),
    ("I'll skip it, continue with my wardrobe", "skip"),
    ("这件太贵了", None), ("不买了吗？", None), ("那先不买了，不过又想买了", None),
    ("如果先不买了，怎么搭", None), ("是不是不买比较好", None), ("我不是不想买", None),
])
def test_only_explicit_unambiguous_purchase_actions_count(question: str, choice: str | None) -> None:
    assert explicit_purchase_choice(question) == choice


@pytest.mark.parametrize("evidence", ["打算买", "不买了", "记住", "记住，我打算买这件", "remember I'll buy this"])
def test_shopping_intent_is_not_independent_style_preference_evidence(evidence: str) -> None:
    assert not has_independent_preference_evidence(evidence)
