"""Feedback-changes-ranking scenario for the recommendation engine (BE#67).

The eval harness validates baked outputs; this drives the ENGINE directly to
prove a derived feedback signal (dismissed/disliked outfit suppression) actually
changes ranking — criterion: "evals include at least one scenario where feedback
changes ranking". No DB.
"""

from __future__ import annotations

from app.application.recommendations.engine import EngineItem, RecommendationEngine


def _items() -> list[EngineItem]:
    # Two tops + two bottoms + a shoe → several SAFE combos, so suppressing the
    # top-ranked look leaves real alternatives for the slot.
    return [
        EngineItem("t1", "Top one", "top", "white"),
        EngineItem("t2", "Top two", "top", "navy"),
        EngineItem("b1", "Bottom one", "bottom", "black"),
        EngineItem("b2", "Bottom two", "bottom", "beige"),
        EngineItem("s1", "Shoe one", "shoes", "brown"),
    ]


async def test_suppressing_a_disliked_look_changes_ranking() -> None:
    engine = RecommendationEngine()
    items = _items()

    baseline = await engine.generate(items)
    assert baseline.outfits
    top = baseline.outfits[0]
    disliked_signature = set(top.item_ids)

    suppressed = await engine.generate(items, suppressed_outfit_item_ids=[disliked_signature])
    assert suppressed.outfits

    chosen_signatures = [set(o.item_ids) for o in suppressed.outfits]
    # The disliked look is down-ranked: either displaced from the selection, or —
    # if it is the only option for its slot — it carries the suppression penalty
    # and a strictly lower score than before.
    if disliked_signature in chosen_signatures:
        penalized = next(o for o in suppressed.outfits if set(o.item_ids) == disliked_signature)
        assert penalized.score_breakdown.get("feedback_suppression_penalty")
        assert penalized.score_total < top.score_total
    else:
        # Replaced by a different look for that slot — ranking changed.
        assert disliked_signature not in chosen_signatures


async def test_no_suppression_signal_leaves_ranking_unchanged() -> None:
    engine = RecommendationEngine()
    items = _items()
    a = await engine.generate(items)
    b = await engine.generate(items, suppressed_outfit_item_ids=[])
    assert [o.item_ids for o in a.outfits] == [o.item_ids for o in b.outfits]
