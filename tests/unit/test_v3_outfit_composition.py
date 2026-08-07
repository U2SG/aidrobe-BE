"""V3-006 — the composition rules, as deterministic fixtures.

The pure layer takes a `ParsedItem` plus `OwnedItemFacts` projections, so every
grounding rule can be asserted exactly, with no container and no fixture
database. What only Postgres can prove — that the availability filter really
filters, that `fit_feedback` joins to the right item, and that a snapshot
survives the wardrobe it described — is in
`tests/services/test_v3_outfit_evidence.py`.

Runs under `pytest --noconftest`.
"""

from __future__ import annotations

import ast
import uuid
from pathlib import Path
from typing import Any

import pytest

from app.application.purchase.candidate_normalizer import ParsedItem
from app.application.purchase.outfit_composition import (
    MAX_OUTFITS,
    OUTFIT_EVIDENCE_VERSION,
    OUTFIT_STATUS_COMPOSED,
    OUTFIT_STATUS_INCOMPLETE,
    OUTFIT_STATUS_NO_AVAILABLE_ITEMS,
    OUTFIT_STATUS_UNKNOWN_CANDIDATE,
    REASON_COLOR_CONFLICT,
    REASON_COMPLETE_LOOK,
    REASON_NEUTRAL_ANCHOR,
    REASON_SHARED_SCENE,
    SLOT_FEET,
    SLOT_LEGS,
    SLOT_TORSO,
    VOCABULARIES,
    OutfitEvidence,
    build_outfit_evidence,
    replay_outfit_evidence,
)
from app.application.purchase.wardrobe_similarity import (
    SCENE_SOURCE_ITEM_TAGS,
    SCENE_SOURCE_WEAR_FEEDBACK,
    OwnedItemFacts,
)
from app.common.enums import Category

# Fixed ids keep the ordering assertions readable: where the tie-break matters
# the test says so, rather than depending on whatever uuid4 produced.
_IDS = [uuid.UUID(int=i) for i in range(1, 40)]


def _facts(
    index: int,
    category: str,
    *,
    color: str = "black",
    style_tags: tuple[str, ...] = (),
    wear_scenes: tuple[str, ...] = (),
) -> OwnedItemFacts:
    return OwnedItemFacts(
        item_id=_IDS[index],
        category=category,
        color_main=color,
        style_tags=style_tags,
        wear_scenes=wear_scenes,
    )


def _candidate(
    category: str = Category.TOP.value,
    *,
    colors: list[str] | None = None,
    scenes: list[str] | None = None,
) -> ParsedItem:
    return ParsedItem(category=category, colors=colors or ["gray"], scene_tags=scenes)


def _all_item_ids(evidence: OutfitEvidence) -> set[uuid.UUID]:
    """Every id the evidence asks the user to wear, outfits and alternatives.

    Acceptance 1 is about *every* suggested supporting item, so the grounding
    assertions below check this union rather than the first outfit.
    """
    ids: set[uuid.UUID] = set()
    for outfit in (*evidence.outfits, *evidence.alternatives):
        ids.update(outfit.item_ids)
    return ids


# --- Grounding (acceptance 1) --------------------------------------------


def test_every_supporting_item_comes_from_the_available_pool() -> None:
    pool = [
        _facts(0, Category.BOTTOM.value),
        _facts(1, Category.SHOES.value),
        _facts(2, Category.OUTERWEAR.value),
    ]

    evidence = build_outfit_evidence(_candidate(), pool)

    assert evidence.status == OUTFIT_STATUS_COMPOSED
    assert _all_item_ids(evidence) <= {f.item_id for f in pool}
    assert _all_item_ids(evidence), "the outfit must actually name items"


def test_a_candidate_top_is_completed_with_a_bottom_and_shoes() -> None:
    pool = [_facts(0, Category.BOTTOM.value), _facts(1, Category.SHOES.value)]

    outfit = build_outfit_evidence(_candidate(Category.TOP.value), pool).outfits[0]

    assert {s.slot for s in outfit.slots} == {SLOT_LEGS, SLOT_FEET}
    # The candidate itself is not owned, so it has no wardrobe id to emit.
    assert outfit.anchor_item_id is None
    assert REASON_COMPLETE_LOOK in outfit.match_reasons


def test_a_dress_candidate_only_needs_shoes_to_be_complete() -> None:
    """A dress covers torso and legs, so demanding a top would be a false gap."""
    pool = [_facts(0, Category.SHOES.value)]

    evidence = build_outfit_evidence(_candidate(Category.DRESS.value), pool)

    assert evidence.status == OUTFIT_STATUS_COMPOSED
    assert evidence.missing_categories == ()
    assert [s.slot for s in evidence.outfits[0].slots] == [SLOT_FEET]


def test_an_owned_dress_can_cover_torso_and_legs_for_a_shoes_candidate() -> None:
    pool = [_facts(0, Category.DRESS.value)]

    evidence = build_outfit_evidence(_candidate(Category.SHOES.value), pool)

    assert evidence.status == OUTFIT_STATUS_COMPOSED
    slots = evidence.outfits[0].slots
    assert {s.slot for s in slots} == {SLOT_TORSO, SLOT_LEGS}
    # One garment, two slots — not two suggestions.
    assert len({s.item_id for s in slots}) == 1


def test_a_dress_is_never_offered_alongside_a_bottom_candidate() -> None:
    """The candidate already occupies the legs slot the dress would need."""
    pool = [_facts(0, Category.DRESS.value), _facts(1, Category.SHOES.value)]

    evidence = build_outfit_evidence(_candidate(Category.BOTTOM.value), pool)

    assert _IDS[0] not in _all_item_ids(evidence)
    assert evidence.status == OUTFIT_STATUS_INCOMPLETE
    assert evidence.missing_categories == (Category.TOP.value,)


# --- Gaps and honesty ----------------------------------------------------


def test_a_wardrobe_without_shoes_reports_the_gap_rather_than_inventing_one() -> None:
    pool = [_facts(0, Category.BOTTOM.value)]

    evidence = build_outfit_evidence(_candidate(Category.TOP.value), pool)

    assert evidence.status == OUTFIT_STATUS_INCOMPLETE
    assert evidence.outfits == ()
    assert evidence.missing_slots == (SLOT_FEET,)
    assert evidence.missing_categories == (Category.SHOES.value,)


def test_a_colour_clash_ranks_last_but_never_manufactures_a_gap() -> None:
    """Excluding clashing items would tell a user who owns shoes to buy shoes."""
    pool = [
        _facts(0, Category.BOTTOM.value, color="green"),
        _facts(1, Category.SHOES.value, color="green"),
    ]

    evidence = build_outfit_evidence(_candidate(Category.TOP.value, colors=["red"]), pool)

    assert evidence.status == OUTFIT_STATUS_COMPOSED
    assert evidence.missing_categories == ()
    # ...and the clash is declared rather than quietly shipped as a good match.
    assert REASON_COLOR_CONFLICT in evidence.outfits[0].match_reasons


def test_a_neutral_item_outranks_a_clashing_one_within_a_slot() -> None:
    pool = [
        _facts(0, Category.BOTTOM.value, color="green"),
        _facts(1, Category.BOTTOM.value, color="black"),
        _facts(2, Category.SHOES.value, color="white"),
    ]

    outfit = build_outfit_evidence(_candidate(colors=["red"]), pool).outfits[0]

    legs = next(s for s in outfit.slots if s.slot == SLOT_LEGS)
    assert legs.item_id == _IDS[1]
    assert REASON_NEUTRAL_ANCHOR in outfit.match_reasons


# --- Scene grounding -----------------------------------------------------


def test_a_worn_in_scene_outranks_a_style_tag_and_is_labelled_as_such() -> None:
    pool = [
        _facts(0, Category.BOTTOM.value, style_tags=("commute",)),
        _facts(1, Category.BOTTOM.value, wear_scenes=("commute",)),
        _facts(2, Category.SHOES.value),
    ]

    outfit = build_outfit_evidence(_candidate(scenes=["commute"]), pool).outfits[0]

    legs = next(s for s in outfit.slots if s.slot == SLOT_LEGS)
    assert legs.item_id == _IDS[1]
    assert outfit.scene == "commute"
    assert outfit.scene_source == SCENE_SOURCE_WEAR_FEEDBACK
    assert REASON_SHARED_SCENE in outfit.match_reasons


def test_a_style_tag_alone_still_grounds_a_scene_at_the_weaker_tier() -> None:
    pool = [
        _facts(0, Category.BOTTOM.value, style_tags=("commute",)),
        _facts(1, Category.SHOES.value),
    ]

    outfit = build_outfit_evidence(_candidate(scenes=["commute"]), pool).outfits[0]

    assert (outfit.scene, outfit.scene_source) == ("commute", SCENE_SOURCE_ITEM_TAGS)


def test_no_scene_evidence_means_no_scene_claim() -> None:
    """Same category is not scene evidence here — it would hold for everything."""
    pool = [_facts(0, Category.BOTTOM.value), _facts(1, Category.SHOES.value)]

    outfit = build_outfit_evidence(_candidate(scenes=["commute"]), pool).outfits[0]

    assert outfit.scene is None
    assert outfit.scene_source is None
    assert REASON_SHARED_SCENE not in outfit.match_reasons


# --- Variation -----------------------------------------------------------


def test_a_second_outfit_is_a_real_variation() -> None:
    pool = [
        _facts(0, Category.BOTTOM.value),
        _facts(1, Category.BOTTOM.value, color="navy"),
        _facts(2, Category.SHOES.value),
    ]

    outfits = build_outfit_evidence(_candidate(), pool).outfits

    assert len(outfits) == MAX_OUTFITS
    assert {o.item_ids for o in outfits}.__len__() == MAX_OUTFITS


def test_one_option_per_slot_yields_one_outfit_not_a_duplicate() -> None:
    pool = [_facts(0, Category.BOTTOM.value), _facts(1, Category.SHOES.value)]

    assert len(build_outfit_evidence(_candidate(), pool).outfits) == 1


# --- Existing-item alternatives (acceptance 2) ---------------------------


def test_a_redundant_candidate_yields_an_owned_alternative() -> None:
    owned_top = _facts(3, Category.TOP.value)
    pool = [owned_top, _facts(0, Category.BOTTOM.value), _facts(1, Category.SHOES.value)]

    evidence = build_outfit_evidence(_candidate(), pool, similar_item_ids=(owned_top.item_id,))

    assert len(evidence.alternatives) == 1
    alternative = evidence.alternatives[0]
    assert alternative.anchor_item_id == owned_top.item_id
    # The anchor is owned, so unlike the candidate it appears in the outfit.
    assert owned_top.item_id in alternative.item_ids
    assert set(alternative.item_ids) <= {f.item_id for f in pool}


def test_an_alternative_anchor_that_is_not_available_is_skipped() -> None:
    """V3-005 counts a coat in the laundry as owned; it is still unwearable.

    The DB layer never loads it, so the pure layer simply cannot find the id —
    this pins that the lookup fails closed instead of fabricating an entry.
    """
    unavailable = _IDS[9]
    pool = [_facts(0, Category.BOTTOM.value), _facts(1, Category.SHOES.value)]

    evidence = build_outfit_evidence(_candidate(), pool, similar_item_ids=(unavailable,))

    assert evidence.alternatives == ()
    assert unavailable not in _all_item_ids(evidence)


def test_alternatives_follow_the_order_v3_005_ranked_them_in() -> None:
    first, second = _facts(3, Category.TOP.value), _facts(4, Category.TOP.value, color="navy")
    pool = [first, second, _facts(0, Category.BOTTOM.value), _facts(1, Category.SHOES.value)]

    evidence = build_outfit_evidence(
        _candidate(), pool, similar_item_ids=(second.item_id, first.item_id)
    )

    assert [a.anchor_item_id for a in evidence.alternatives] == [second.item_id, first.item_id]


def test_no_similar_items_means_no_alternatives() -> None:
    pool = [_facts(0, Category.BOTTOM.value), _facts(1, Category.SHOES.value)]

    assert build_outfit_evidence(_candidate(), pool).alternatives == ()


# --- Degradation ---------------------------------------------------------


def test_an_unparsed_candidate_is_unknown_rather_than_empty() -> None:
    """`unknown_candidate` and `no_available_items` must stay distinguishable so
    V3-007 can reach `insufficient_information` honestly."""
    evidence = build_outfit_evidence(ParsedItem(), [_facts(0, Category.SHOES.value)])

    assert evidence.status == OUTFIT_STATUS_UNKNOWN_CANDIDATE
    assert evidence.outfits == ()


def test_an_empty_wardrobe_says_so() -> None:
    evidence = build_outfit_evidence(_candidate(), [])

    assert evidence.status == OUTFIT_STATUS_NO_AVAILABLE_ITEMS


def test_a_category_outside_the_outfit_vocabulary_composes_nothing() -> None:
    """Otherwise every slot reads as open and the composer returns a complete
    look the candidate is not part of — a grounded outfit that grounds nothing.

    `parsed.category` is normally a `Category` member, but V3-004 passes a
    provider's reading straight through, so this input is reachable.
    """
    pool = [
        _facts(0, Category.TOP.value),
        _facts(1, Category.BOTTOM.value),
        _facts(2, Category.SHOES.value),
    ]

    evidence = build_outfit_evidence(_candidate("accessory"), pool)

    assert evidence.status == OUTFIT_STATUS_UNKNOWN_CANDIDATE
    assert evidence.outfits == ()
    assert _all_item_ids(evidence) == set()


def test_a_truncated_scan_is_declared_in_the_payload() -> None:
    pool = [_facts(0, Category.BOTTOM.value), _facts(1, Category.SHOES.value)]

    evidence = build_outfit_evidence(_candidate(), pool, scan_truncated=True)

    assert evidence.as_payload()["scan_truncated"] is True


# --- Determinism and shape (acceptance 4 groundwork) ---------------------


def test_composition_is_deterministic() -> None:
    """Two runs over the same wardrobe must agree, or V3-007's immutable
    snapshot would depend on when it happened to be taken."""
    pool = [
        _facts(0, Category.BOTTOM.value),
        _facts(1, Category.BOTTOM.value, color="navy"),
        _facts(2, Category.SHOES.value),
        _facts(3, Category.TOP.value),
        _facts(4, Category.OUTERWEAR.value),
    ]
    ids = (_IDS[3],)

    first = build_outfit_evidence(_candidate(), pool, similar_item_ids=ids)
    second = build_outfit_evidence(_candidate(), pool, similar_item_ids=ids)

    assert first.as_payload() == second.as_payload()


def _leaves(node: Any) -> list[Any]:
    if isinstance(node, dict):
        return [leaf for value in node.values() for leaf in _leaves(value)]
    if isinstance(node, list):
        return [leaf for value in node for leaf in _leaves(value)]
    return [node]


def test_the_payload_carries_no_numeric_score() -> None:
    """V3-005 acceptance 3's anti-goal, kept in V3-006's payload too: ranking
    weights exist, but nothing that could render as "87% match" is emitted."""
    pool = [
        _facts(0, Category.BOTTOM.value, wear_scenes=("commute",)),
        _facts(1, Category.SHOES.value, color="white"),
        _facts(3, Category.TOP.value),
    ]
    payload = build_outfit_evidence(
        _candidate(scenes=["commute"]), pool, similar_item_ids=(_IDS[3],)
    ).as_payload()

    # Assert the walk has something to walk, so this cannot pass by describing
    # an empty payload.
    assert payload["outfits"] and payload["alternatives"]

    allowed = {value for values in VOCABULARIES.values() for value in values}
    for leaf in _leaves(payload):
        assert not isinstance(leaf, float), f"a float reached the payload: {leaf!r}"
        # `isinstance(True, int)` is True, so bools must be checked first or a
        # flag would be waved through as a count.
        if isinstance(leaf, bool) or leaf is None:
            continue
        if isinstance(leaf, int):
            continue
        assert isinstance(leaf, str)
        if leaf in allowed:
            continue
        uuid.UUID(leaf)  # the only other legal leaf is an item id


def test_replay_round_trips_the_payload_byte_for_byte() -> None:
    pool = [
        _facts(0, Category.BOTTOM.value, wear_scenes=("commute",)),
        _facts(1, Category.SHOES.value),
        _facts(3, Category.TOP.value),
    ]
    payload = build_outfit_evidence(
        _candidate(scenes=["commute"]), pool, similar_item_ids=(_IDS[3],)
    ).as_payload()

    assert replay_outfit_evidence(payload).as_payload() == payload


def test_replay_refuses_an_unreadable_version() -> None:
    """Guessing at a newer shape would put a wrong explanation in front of a
    user; V3-003's versioning exists so old rows are never rewritten instead."""
    payload = build_outfit_evidence(_candidate(), [_facts(0, Category.SHOES.value)]).as_payload()
    payload["evidence_version"] = OUTFIT_EVIDENCE_VERSION + 1

    with pytest.raises(ValueError, match="evidence_version"):
        replay_outfit_evidence(payload)


# --- Acceptance 3: no visual generation ----------------------------------

# Modules that would mean the engine depends on a provider call. The LLM
# *sanitizer* is not here on purpose: `candidate_normalizer` uses it to bound
# untrusted text, which is the opposite of calling a model.
_PROVIDER_MODULES = (
    "app.application.ports.image_generation",
    "app.application.ports.vision",
    "app.application.ports.ocr",
    "app.application.ports.llm",
    "app.infrastructure.vision",
    "app.infrastructure.workers",
)
_APP_ROOT = Path(__file__).resolve().parents[2]


def _module_path(module: str) -> Path | None:
    base = _APP_ROOT / Path(*module.split("."))
    for candidate in (base.with_suffix(".py"), base / "__init__.py"):
        if candidate.is_file():
            return candidate
    return None


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            found.add(node.module)
    return {name for name in found if name.startswith("app.")}


def _reachable(entry: str) -> set[str]:
    """Every first-party module reachable from `entry`, transitively."""
    seen: set[str] = set()
    queue = [entry]
    while queue:
        module = queue.pop()
        if module in seen:
            continue
        seen.add(module)
        path = _module_path(module)
        if path is not None:
            queue.extend(_imports(path))
    return seen


@pytest.mark.parametrize(
    "entry",
    [
        "app.application.purchase.outfit_composition",
        "app.application.purchase.compose_outfit_evidence",
    ],
)
def test_the_engine_reaches_no_provider_port(entry: str) -> None:
    """Asserted over the import graph, not claimed in a docstring: nothing the
    engine can reach is an image, vision, OCR or LLM provider, or the async job
    runtime that pays for one. A grounded outfit is text and item ids."""
    reachable = _reachable(entry)

    assert entry in reachable, "the walker must have actually resolved the entry point"
    offenders = {
        module
        for module in reachable
        for provider in _PROVIDER_MODULES
        if module == provider or module.startswith(f"{provider}.")
    }
    assert offenders == set()


def test_the_pure_layer_touches_no_database() -> None:
    """The V3-005 split, kept: composition is a function of projections, which
    is what lets every rule above be a fixture rather than a container."""
    path = _module_path("app.application.purchase.outfit_composition")
    assert path is not None
    source = path.read_text(encoding="utf-8")

    assert "sqlalchemy" not in source
    assert "AsyncSession" not in source
