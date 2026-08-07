"""V3-005 — deterministic fixtures for the pure similarity/redundancy layer.

No database and no conftest: `app.application.purchase.wardrobe_similarity`
imports nothing from `app.infrastructure`, so this module runs under
`pytest --noconftest` (the session-scoped autouse `create_schema` fixture in
`tests/conftest.py` would otherwise start a Postgres container for what are
pure functions). That is the same arrangement V3-004 used for the sanitizer.

Coverage map, against V3-005's acceptance criteria:

- A1 structured attributes + deterministic fallback → `test_rank_*`
- A2 similar items + overlapping scenarios → `test_evidence_*`, `test_coverage_*`
- A3 no misleading suitability percentage → `test_payload_carries_no_numeric_score`
- A4 pgvector on/off → `test_external_order_is_preserved` covers the vector
  tier's output handling; branch selection and degradation are in
  `tests/services/test_v3_similarity_evidence.py`.
"""

from __future__ import annotations

import uuid
from typing import Any

from app.application.purchase.candidate_normalizer import SCENE_TAGS, ParsedItem
from app.application.purchase.compute_similarity_evidence import order_by_ids
from app.application.purchase.wardrobe_similarity import (
    MAX_SIMILAR_ITEMS,
    REASON_SAME_CATEGORY,
    REASON_SAME_COLOR,
    REASON_SHARED_MATERIAL,
    REASON_SHARED_SCENE,
    REDUNDANCY_HIGH,
    REDUNDANCY_LOW,
    REDUNDANCY_MEDIUM,
    REDUNDANCY_NONE,
    REDUNDANCY_UNKNOWN,
    RELATION_COMPLEMENTS,
    RELATION_COVERS,
    SCENE_SOURCE_CATEGORY_DEFAULT,
    SCENE_SOURCE_ITEM_TAGS,
    SCENE_SOURCE_WEAR_FEEDBACK,
    SHARED_SCENES,
    VOCABULARIES,
    OwnedItemFacts,
    build_evidence,
    match_reasons,
    normalize_style_tags,
    rank_by_attributes,
)
from app.common.enums import Category, FitScene

# Fixed ids so ordering assertions are about the ranking rule, not about which
# uuid4 happened to sort first. Ascending by construction, which makes the
# `str(item_id)` tie-break in `rank_by_attributes` predictable and readable.
ITEM_A = uuid.UUID("00000000-0000-4000-8000-00000000000a")
ITEM_B = uuid.UUID("00000000-0000-4000-8000-00000000000b")
ITEM_C = uuid.UUID("00000000-0000-4000-8000-00000000000c")
ITEM_D = uuid.UUID("00000000-0000-4000-8000-00000000000d")


def _candidate(**overrides: Any) -> ParsedItem:
    """A gray wool oversized commute coat, unless a test says otherwise."""
    base: dict[str, Any] = {
        "category": Category.OUTERWEAR.value,
        "colors": ["gray"],
        "silhouette": "oversized",
        "material_hints": ["wool"],
        "price": "¥1299",
        "scene_tags": ["commute"],
    }
    base.update(overrides)
    return ParsedItem(**base)


def _owned(item_id: uuid.UUID, **overrides: Any) -> OwnedItemFacts:
    base: dict[str, Any] = {
        "item_id": item_id,
        "category": Category.OUTERWEAR.value,
        "color_main": "navy",
    }
    base.update(overrides)
    return OwnedItemFacts(**base)


# --- The scene vocabulary bridge ----------------------------------------


def test_shared_scenes_is_a_real_non_empty_intersection() -> None:
    """The bridge V3-005 evidences scene overlap across must actually exist.

    `SHARED_SCENES` is computed as `FitScene ∩ SCENE_TAGS`. If either
    vocabulary is edited apart from the other, scene coverage silently degrades
    to "no shared scenes" and acceptance 2 ships hollow — so assert the
    intersection is non-empty AND that every `FitScene` member survives it.
    """
    assert SHARED_SCENES
    assert SHARED_SCENES == {s.value for s in FitScene}
    assert SHARED_SCENES <= set(SCENE_TAGS)


# --- A1: structured attributes and deterministic fallback ---------------


def test_match_reasons_report_every_shared_attribute() -> None:
    facts = _owned(
        ITEM_A,
        color_main="gray",
        material="wool",
        wear_scenes=("commute",),
    )
    assert match_reasons(_candidate(), facts) == (
        REASON_SAME_CATEGORY,
        REASON_SAME_COLOR,
        REASON_SHARED_MATERIAL,
        REASON_SHARED_SCENE,
    )


def test_match_reasons_on_a_bare_category_match() -> None:
    assert match_reasons(_candidate(), _owned(ITEM_A)) == (REASON_SAME_CATEGORY,)


def test_rank_puts_the_strongest_structured_match_first() -> None:
    weakest = _owned(ITEM_A)
    scene_only = _owned(ITEM_B, wear_scenes=("commute",))
    color_match = _owned(ITEM_C, color_main="gray")
    ranked = rank_by_attributes(_candidate(), [weakest, scene_only, color_match])
    assert [f.item_id for f in ranked] == [ITEM_C, ITEM_B, ITEM_A]


def test_rank_is_stable_and_independent_of_input_order() -> None:
    """Two items matching equally well must always come back in the same order,
    or the same wardrobe would produce a different snapshot on every call."""
    candidate = _candidate()
    first = _owned(ITEM_A, color_main="gray")
    second = _owned(ITEM_B, color_main="gray")
    assert [f.item_id for f in rank_by_attributes(candidate, [first, second])] == [ITEM_A, ITEM_B]
    assert [f.item_id for f in rank_by_attributes(candidate, [second, first])] == [ITEM_A, ITEM_B]


def test_external_order_is_preserved_into_the_payload() -> None:
    """A4 — the vector tier's output handling, without needing the extension.

    `order_by_ids` applies an order the structured rule would never produce
    (weakest match first). `build_evidence` must honour it: that is how a
    pgvector ranking reaches the payload, and it proves the pure layer does not
    silently re-sort whatever the DB tier decided.
    """
    weakest = _owned(ITEM_A)
    strongest = _owned(ITEM_B, color_main="gray", material="wool")
    ordered = order_by_ids([weakest, strongest], [ITEM_A, ITEM_B])
    evidence = build_evidence(_candidate(), ordered)
    assert [s.item_id for s in evidence.similar_items] == [ITEM_A, ITEM_B]
    # And the band follows the ranking's top item, not the best item overall.
    assert evidence.redundancy_level == REDUNDANCY_LOW


def test_order_by_ids_keeps_unranked_items_after_the_ranked_ones() -> None:
    ranked_only = _owned(ITEM_A)
    unranked = _owned(ITEM_B)
    assert [f.item_id for f in order_by_ids([unranked, ranked_only], [ITEM_A])] == [ITEM_A, ITEM_B]


def test_normalize_style_tags_accepts_both_persisted_shapes() -> None:
    assert normalize_style_tags({"tags": ["Formal", " casual "]}) == ("formal", "casual")
    assert normalize_style_tags(["Minimal"]) == ("minimal",)
    assert normalize_style_tags(None) == ()
    assert normalize_style_tags({"unexpected": 1}) == ()


# --- A2: similar items and overlapping scenarios ------------------------


def test_evidence_separates_covers_from_complements() -> None:
    same_category = _owned(ITEM_A, color_main="gray")
    complement = _owned(ITEM_B, category=Category.BOTTOM.value)
    unrelated = _owned(ITEM_C, category=Category.DRESS.value)
    evidence = build_evidence(_candidate(), [same_category, complement, unrelated])

    assert evidence.similar_item_count == 1
    assert evidence.compatible_item_ids == [ITEM_B]
    relations = {s.item_id: s.relation for s in evidence.similar_items}
    assert relations == {ITEM_A: RELATION_COVERS, ITEM_B: RELATION_COMPLEMENTS}
    # `dress` is neither the candidate's category nor one of its complements.
    assert ITEM_C not in relations


def test_evidence_caps_the_displayed_list_but_not_the_count() -> None:
    """The list is a display slice; the count is what the user owns.

    Reporting `len(similar_items)` as `similar_item_count` would tell someone
    with 8 gray coats that they own 5 — understating redundancy, which is the
    buy-encouraging direction. §10.3 reads the field as a count of the wardrobe,
    not as the length of a list the decision page happens to render.
    """
    owned = [
        OwnedItemFacts(item_id=uuid.UUID(int=i), category=Category.OUTERWEAR.value, color_main="gray")
        for i in range(MAX_SIMILAR_ITEMS + 3)
    ]
    evidence = build_evidence(_candidate(), owned)

    assert evidence.similar_item_count == MAX_SIMILAR_ITEMS + 3
    assert len(evidence.similar_items) == MAX_SIMILAR_ITEMS
    assert evidence.as_payload()["similar_item_count"] == MAX_SIMILAR_ITEMS + 3
    # A complete scan is not a truncated one, whatever the display cap did.
    assert evidence.same_category_scan_truncated is False


def test_a_saturated_scan_marks_the_count_as_a_floor() -> None:
    """`similar_item_count` is only a total when the scan window was not full.

    The DB layer bounds its same-category window; if that window saturated, the
    number here is "at least this many". Emitting it bare would restate the
    truncation bug this flag exists to make visible.
    """
    evidence = build_evidence(
        _candidate(),
        [_owned(ITEM_A, color_main="gray")],
        same_category_truncated=True,
    )

    assert evidence.same_category_scan_truncated is True
    assert evidence.as_payload()["same_category_scan_truncated"] is True


def test_coverage_prefers_wear_feedback_over_weaker_sources() -> None:
    """Real wear evidence must not be diluted by items that only match by
    category — the strongest tier wins and reports only its own items."""
    rated = _owned(ITEM_A, wear_scenes=("commute",))
    tagged = _owned(ITEM_B, style_tags=("commute",))
    default_only = _owned(ITEM_C)
    evidence = build_evidence(_candidate(), [rated, tagged, default_only])

    assert len(evidence.coverage_scenarios) == 1
    coverage = evidence.coverage_scenarios[0]
    assert coverage.scene == "commute"
    assert coverage.source == SCENE_SOURCE_WEAR_FEEDBACK
    assert coverage.covered_by_item_ids == (ITEM_A,)


def test_coverage_falls_back_to_item_tags_then_category() -> None:
    tagged = build_evidence(_candidate(), [_owned(ITEM_A, style_tags=("commute",))])
    assert tagged.coverage_scenarios[0].source == SCENE_SOURCE_ITEM_TAGS

    bare = build_evidence(_candidate(), [_owned(ITEM_A)])
    assert bare.coverage_scenarios[0].source == SCENE_SOURCE_CATEGORY_DEFAULT


def test_coverage_quantifies_how_many_items_overlap() -> None:
    """"Quantify overlapping use-cases" (A2) is a count, not a score."""
    evidence = build_evidence(
        _candidate(),
        [
            _owned(ITEM_A, wear_scenes=("commute",)),
            _owned(ITEM_B, wear_scenes=("commute",)),
            _owned(ITEM_C, wear_scenes=("travel",)),
        ],
    )
    coverage = {c.scene: c for c in evidence.coverage_scenarios}
    assert set(coverage["commute"].covered_by_item_ids) == {ITEM_A, ITEM_B}
    assert coverage["commute"].as_payload()["covered_by_item_count"] == 2


def test_scene_outside_the_shared_vocabulary_gets_no_invented_coverage() -> None:
    """`wedding` is a candidate-side scene with no owned-side counterpart, so
    the category default must not claim it. Absence of evidence is reported as
    exactly that, rather than as coverage."""
    assert "wedding" not in SHARED_SCENES
    evidence = build_evidence(_candidate(scene_tags=["wedding"]), [_owned(ITEM_A)])
    assert evidence.coverage_scenarios == ()
    assert evidence.scenarios_without_evidence == ("wedding",)


def test_coverage_is_judged_against_same_category_items_only() -> None:
    """A complementary item pairs with the candidate; it does not do its job,
    so it can never be evidence that the candidate's scene is already covered."""
    complement = _owned(ITEM_B, category=Category.BOTTOM.value, wear_scenes=("commute",))
    evidence = build_evidence(_candidate(), [complement])
    assert evidence.coverage_scenarios == ()
    assert evidence.scenarios_without_evidence == ("commute",)


def test_scene_order_follows_the_candidate_and_deduplicates() -> None:
    evidence = build_evidence(
        _candidate(scene_tags=["travel", "commute", "travel"]),
        [_owned(ITEM_A)],
    )
    assert [c.scene for c in evidence.coverage_scenarios] == ["travel", "commute"]


# --- Redundancy banding -------------------------------------------------


def test_redundancy_bands_follow_the_top_match() -> None:
    assert build_evidence(_candidate(), [_owned(ITEM_A, color_main="gray")]).redundancy_level == REDUNDANCY_HIGH
    assert build_evidence(_candidate(), [_owned(ITEM_A, material="wool")]).redundancy_level == REDUNDANCY_MEDIUM
    assert build_evidence(_candidate(), [_owned(ITEM_A)]).redundancy_level == REDUNDANCY_LOW


def test_empty_wardrobe_is_none_but_an_unparsed_candidate_is_unknown() -> None:
    """The distinction V3-007 needs to reach `insufficient_information`
    honestly: `none` means we searched and found nothing, `unknown` means we
    never had a category to search with."""
    assert build_evidence(_candidate(), []).redundancy_level == REDUNDANCY_NONE

    for degraded in (None, ParsedItem(), ParsedItem(colors=["gray"], price="¥1299")):
        evidence = build_evidence(degraded, [_owned(ITEM_A, color_main="gray")])
        assert evidence.redundancy_level == REDUNDANCY_UNKNOWN
        assert evidence.similar_items == ()
        assert evidence.coverage_scenarios == ()


def test_build_evidence_tolerates_partial_and_malformed_candidate_fields() -> None:
    """A degraded V3-004 parse must never break the decision flow."""
    messy = ParsedItem(
        category=Category.OUTERWEAR.value,
        colors=None,
        material_hints=None,
        scene_tags=["", "  ", "commute"],
    )
    evidence = build_evidence(messy, [_owned(ITEM_A, color_main="gray")])
    assert evidence.redundancy_level == REDUNDANCY_LOW
    assert [c.scene for c in evidence.coverage_scenarios] == ["commute"]


def test_unknown_category_has_no_complements_and_still_returns() -> None:
    evidence = build_evidence(_candidate(category="accessory"), [_owned(ITEM_A)])
    assert evidence.redundancy_level == REDUNDANCY_NONE
    assert evidence.similar_items == ()


# --- A3: no misleading universal suitability percentage -----------------


def _leaves(value: Any, path: str = "") -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        return [leaf for k, v in value.items() for leaf in _leaves(v, f"{path}.{k}")]
    if isinstance(value, list):
        return [leaf for i, v in enumerate(value) for leaf in _leaves(v, f"{path}[{i}]")]
    return [(path, value)]


def test_payload_carries_no_numeric_score() -> None:
    """A3 — structural, not a promise.

    Walk a fully-populated payload and assert every leaf is a bounded literal,
    an id, an integer count, or the version. Any float at all fails: a float in
    this payload is a suitability score by another name, and V3_SPEC §5.4 rules
    it out ("避免虚假的精确评分，如'适合度 87 分'"). Because it inspects the
    rendered payload rather than the source, adding a scored field later cannot
    pass this test quietly.
    """
    evidence = build_evidence(
        _candidate(scene_tags=["commute", "wedding"]),
        [
            _owned(ITEM_A, color_main="gray", material="wool", wear_scenes=("commute",)),
            _owned(ITEM_B),
            _owned(ITEM_C, category=Category.BOTTOM.value),
            _owned(ITEM_D, category=Category.SHOES.value),
        ],
    )
    payload = evidence.as_payload()
    # Fully populated, or the walk below would prove nothing.
    assert payload["similar_items"] and payload["coverage_scenarios"]
    assert payload["compatible_item_ids"] and payload["scenarios_without_evidence"]

    allowed = set().union(*VOCABULARIES.values()) | set(SCENE_TAGS)
    count_keys = {"similar_item_count", "covered_by_item_count", "evidence_version"}
    # Enumerated separately and checked before the int branch, because
    # `isinstance(True, int)` is True in Python — a bool would otherwise be
    # waved through as a "count".
    flag_keys = {"same_category_scan_truncated"}
    for path, leaf in _leaves(payload):
        assert not isinstance(leaf, float), f"{path} is a float: {leaf!r}"
        if isinstance(leaf, bool):
            assert path.rsplit(".", 1)[-1] in flag_keys, f"{path} is an unexpected flag"
            continue
        if isinstance(leaf, int):
            assert path.rsplit(".", 1)[-1] in count_keys, f"{path} is an unexpected number"
            continue
        assert isinstance(leaf, str), f"{path} is neither literal, id nor count: {leaf!r}"
        if path.endswith("item_id") or "item_ids" in path:
            uuid.UUID(leaf)  # raises if it is not an id
            continue
        assert leaf in allowed, f"{path} carries an unfrozen value: {leaf!r}"


def test_payload_has_no_percentage_or_score_shaped_key() -> None:
    evidence = build_evidence(_candidate(), [_owned(ITEM_A, color_main="gray")])
    keys = {path.rsplit(".", 1)[-1] for path, _ in _leaves(evidence.as_payload())}
    for banned in ("score", "percent", "percentage", "confidence", "similarity", "rating", "fit"):
        assert not any(banned in key for key in keys), f"payload exposes a {banned}-shaped key"
