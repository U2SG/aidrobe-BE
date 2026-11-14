"""V3-007 — the decision matrix, without a database.

The whole judgement layer is pure, so the acceptance criteria are testable
here: which of the five states comes out, whether a reason cites evidence that
exists, when `visual_need` is false, and that no rendered sentence claims
anything about a body or a size. `tests/services/test_v3_purchase_decision.py`
re-checks the same behaviour end-to-end against a real Postgres, where the
evidence comes from real rows instead of hand-built fixtures.

Two of these tests are deliberately *not* written as "the mapping table says
X, assert X" — that shape passes by construction. Instead:

- `test_every_number_in_a_reason_came_from_the_evidence` walks the rendered
  copy of a whole grid of evidence and refuses any digit that is not in the
  evidence payload, so a template that invents a count fails.
- the `visual_need` cases are minimal pairs: two inputs differing in exactly
  one field, asserting the flag moves. A parametrization over the five states
  would only restate the implementation.

Runs under `pytest --noconftest`.
"""

from __future__ import annotations

import itertools
import re
import uuid
from typing import Any, get_args

import pytest

from app.application.purchase import purchase_decision_rules as rules
from app.application.purchase.candidate_normalizer import SCENE_TAGS, ParsedItem
from app.application.purchase.outfit_composition import (
    OUTFIT_STATUS_COMPOSED,
    OUTFIT_STATUS_INCOMPLETE,
    OUTFIT_STATUS_NO_AVAILABLE_ITEMS,
    OUTFIT_STATUS_UNKNOWN_CANDIDATE,
    REASON_COLOR_CONFLICT,
    REASON_COMPLETE_LOOK,
    GroundedOutfit,
    OutfitEvidence,
    OutfitSlot,
)
from app.application.purchase.purchase_decision_rules import (
    ACTION_ADD_TO_CANDIDATES,
    ACTION_RECHECK_COLOR,
    ACTION_SKIP,
    ACTION_STYLE_WITH_EXISTING,
    BANNED_CLAIM_TERMS,
    CONCERN_COLOR,
    CONCERN_NONE,
    CONCERN_REDUNDANCY,
    CONCERN_SCENE,
    CONCERN_VOLUME,
    DECISION_VERSION,
    MAX_COMPARISON_ITEMS,
    MAX_REASONS,
    MIN_GROUNDING_ITEMS,
    MIN_REASONS,
    NEXT_ACTIONS,
    REASON_ADOPTION_HISTORY,
    REASON_COMPATIBILITY,
    REASON_EXTRA_PURCHASE_NEEDED,
    REASON_KINDS,
    REASON_REDUNDANCY,
    REASON_SCENE_COVERAGE,
    VOCABULARIES,
    PurchaseDecisionOutcome,
    decide,
    replay_decision,
)
from app.application.purchase.wardrobe_similarity import (
    REASON_SAME_CATEGORY,
    REASON_SAME_COLOR,
    REASON_SHARED_MATERIAL,
    REDUNDANCY_HIGH,
    REDUNDANCY_LOW,
    REDUNDANCY_MEDIUM,
    REDUNDANCY_NONE,
    REDUNDANCY_UNKNOWN,
    RELATION_COMPLEMENTS,
    RELATION_COVERS,
    SCENE_SOURCE_CATEGORY_DEFAULT,
    SCENE_SOURCE_WEAR_FEEDBACK,
    ClosetEvidence,
    SceneCoverage,
    SimilarItemEvidence,
)
from app.common.enums import (
    Category,
    DecisionUncertainty,
    DecisionVisualNeed,
    PurchaseDecision,
)
from app.presentation.api.v1.schemas.purchase_decisions import (
    Decision,
    NextAction,
    ReasonKind,
    Uncertainty,
    VisualNeed,
)

# --- Fixtures ------------------------------------------------------------

# The `match_reasons` shapes V3-005 bands `high` and `medium` from. Named
# because they are the difference between "you own this coat" and "you own
# something like it", which is the difference between two decision states.
_SAME_COLOUR = (REASON_SAME_CATEGORY, REASON_SAME_COLOR)
_SAME_MATERIAL = (REASON_SAME_CATEGORY, REASON_SHARED_MATERIAL)


def _candidate(**overrides: Any) -> ParsedItem:
    defaults: dict[str, Any] = {
        "category": Category.OUTERWEAR.value,
        "colors": ["gray"],
        "scene_tags": ["commute"],
    }
    defaults.update(overrides)
    return ParsedItem(**defaults)


def _closet(
    level: str = REDUNDANCY_NONE,
    *,
    covers: int = 0,
    complements: int = 0,
    count: int | None = None,
    truncated: bool = False,
    top_reasons: tuple[str, ...] = (REASON_SAME_CATEGORY,),
    coverage: tuple[tuple[str, int, str], ...] = (),
    uncovered: tuple[str, ...] = (),
) -> ClosetEvidence:
    similar = [
        SimilarItemEvidence(item_id=uuid.uuid4(), relation=RELATION_COVERS, match_reasons=top_reasons)
        for _ in range(covers)
    ]
    similar.extend(
        SimilarItemEvidence(item_id=uuid.uuid4(), relation=RELATION_COMPLEMENTS, match_reasons=())
        for _ in range(complements)
    )
    return ClosetEvidence(
        redundancy_level=level,
        similar_items=tuple(similar),
        coverage_scenarios=tuple(
            SceneCoverage(
                scene=scene,
                covered_by_item_ids=tuple(uuid.uuid4() for _ in range(n)),
                source=source,
            )
            for scene, n, source in coverage
        ),
        scenarios_without_evidence=uncovered,
        similar_item_count=covers if count is None else count,
        same_category_scan_truncated=truncated,
    )


def _look(*, conflict: bool = False, anchor: uuid.UUID | None = None) -> GroundedOutfit:
    reasons = [REASON_COMPLETE_LOOK]
    if conflict:
        reasons.append(REASON_COLOR_CONFLICT)
    return GroundedOutfit(
        slots=(
            OutfitSlot(slot="torso", item_id=anchor or uuid.uuid4(), category=Category.TOP.value),
            OutfitSlot(slot="legs", item_id=uuid.uuid4(), category=Category.BOTTOM.value),
            OutfitSlot(slot="feet", item_id=uuid.uuid4(), category=Category.SHOES.value),
        ),
        match_reasons=tuple(reasons),
        anchor_item_id=anchor,
    )


def _outfit(
    status: str = OUTFIT_STATUS_COMPOSED,
    *,
    outfits: int = 1,
    alternatives: int = 0,
    conflict: bool = False,
    missing: tuple[str, ...] = (),
    available: int = 12,
    truncated: bool = False,
) -> OutfitEvidence:
    return OutfitEvidence(
        status=status,
        outfits=tuple(_look(conflict=conflict) for _ in range(outfits)),
        alternatives=tuple(_look(anchor=uuid.uuid4()) for _ in range(alternatives)),
        missing_slots=tuple("legs" for _ in missing),
        missing_categories=missing,
        available_item_count=available,
        scan_truncated=truncated,
    )


# The grid every property-style test below runs over. Deliberately includes the
# degenerate corners (unknown candidate, empty wardrobe, saturated scans) —
# those are where a template is most likely to render something unevidenced.
def _grid() -> list[tuple[ParsedItem | None, ClosetEvidence, OutfitEvidence]]:
    closets = [
        _closet(REDUNDANCY_UNKNOWN),
        _closet(REDUNDANCY_NONE),
        _closet(REDUNDANCY_LOW, covers=2),
        _closet(REDUNDANCY_MEDIUM, covers=3, top_reasons=_SAME_MATERIAL),
        _closet(REDUNDANCY_HIGH, covers=5, count=43, truncated=True, top_reasons=_SAME_COLOUR),
        _closet(
            REDUNDANCY_LOW,
            covers=1,
            complements=2,
            coverage=(("commute", 2, SCENE_SOURCE_WEAR_FEEDBACK),),
            uncovered=("wedding",),
        ),
    ]
    outfits = [
        _outfit(OUTFIT_STATUS_UNKNOWN_CANDIDATE, outfits=0, available=0),
        _outfit(OUTFIT_STATUS_NO_AVAILABLE_ITEMS, outfits=0, available=0),
        _outfit(OUTFIT_STATUS_INCOMPLETE, outfits=0, missing=(Category.SHOES.value,), available=3),
        _outfit(),
        _outfit(outfits=2, alternatives=1, conflict=True, truncated=True),
    ]
    parsed = [_candidate(), _candidate(silhouette="oversized"), None]
    return [(p, c, o) for p, c, o in itertools.product(parsed, closets, outfits)]


def _all_outcomes(**kwargs: Any) -> list[PurchaseDecisionOutcome]:
    return [decide(p, c, o, **kwargs) for p, c, o in _grid()]


# --- Acceptance 1: the five bounded states -------------------------------


def test_decision_is_always_one_of_the_five_states() -> None:
    states = {o.decision for o in _all_outcomes()}

    assert states <= {d.value for d in PurchaseDecision}


@pytest.mark.parametrize(
    ("closet", "outfit", "expected"),
    [
        # The candidate never parsed: neither module could look, so we say so.
        (
            _closet(REDUNDANCY_UNKNOWN),
            _outfit(OUTFIT_STATUS_UNKNOWN_CANDIDATE, outfits=0),
            PurchaseDecision.INSUFFICIENT_INFORMATION.value,
        ),
        # Same category AND same colour already owned.
        (
            _closet(REDUNDANCY_HIGH, covers=2, top_reasons=_SAME_COLOUR),
            _outfit(alternatives=1),
            PurchaseDecision.AVOID_DUPLICATE.value,
        ),
        # Nothing similar, but the wardrobe cannot complete a look around it.
        (
            _closet(REDUNDANCY_NONE),
            _outfit(OUTFIT_STATUS_INCOMPLETE, outfits=0, missing=(Category.SHOES.value,)),
            PurchaseDecision.WAIT.value,
        ),
        # Empty wardrobe — also "not yet", never "no".
        (
            _closet(REDUNDANCY_NONE),
            _outfit(OUTFIT_STATUS_NO_AVAILABLE_ITEMS, outfits=0, available=0),
            PurchaseDecision.WAIT.value,
        ),
        # It works, but it clashes with what it would be worn with.
        (
            _closet(REDUNDANCY_NONE),
            _outfit(conflict=True),
            PurchaseDecision.CONSIDER_ALTERNATE.value,
        ),
        # A near-miss in the same category: consider a different colourway.
        (
            _closet(REDUNDANCY_MEDIUM, covers=1, top_reasons=_SAME_MATERIAL),
            _outfit(),
            PurchaseDecision.CONSIDER_ALTERNATE.value,
        ),
        # Composes, nothing similar owned, no clash.
        (
            _closet(REDUNDANCY_NONE),
            _outfit(outfits=2),
            PurchaseDecision.WORTH_ADDING.value,
        ),
    ],
)
def test_the_decision_matrix(closet: ClosetEvidence, outfit: OutfitEvidence, expected: str) -> None:
    assert decide(_candidate(), closet, outfit).decision == expected


def test_every_state_is_reachable_from_the_grid() -> None:
    """Guards the matrix above against silently covering only some states."""
    assert {o.decision for o in _all_outcomes()} == {d.value for d in PurchaseDecision}


def test_a_duplicate_stays_a_duplicate_even_when_no_outfit_can_be_composed() -> None:
    """Ordering is load-bearing: `wait` would read as "buy more things first"
    for a user who already owns the thing — wrong in the expensive direction."""
    outcome = decide(
        _candidate(),
        _closet(REDUNDANCY_HIGH, covers=1, top_reasons=_SAME_COLOUR),
        _outfit(OUTFIT_STATUS_INCOMPLETE, outfits=0, missing=(Category.SHOES.value,)),
    )

    assert outcome.decision == PurchaseDecision.AVOID_DUPLICATE.value


def test_disagreeing_evidence_modules_never_assert_a_decision() -> None:
    """One module knowing the category and the other not is contradictory
    input; asserting anything from it would be a guess."""
    for closet, outfit in (
        (_closet(REDUNDANCY_UNKNOWN), _outfit()),
        (_closet(REDUNDANCY_NONE), _outfit(OUTFIT_STATUS_UNKNOWN_CANDIDATE, outfits=0)),
    ):
        assert decide(_candidate(), closet, outfit).decision == (
            PurchaseDecision.INSUFFICIENT_INFORMATION.value
        )


# --- Acceptance 2: reasons cite evidence, and claim nothing about a body --


def test_there_are_always_two_or_three_reasons_from_the_frozen_vocabulary() -> None:
    for outcome in _all_outcomes():
        assert MIN_REASONS <= len(outcome.reasons) <= MAX_REASONS
        assert {r.kind for r in outcome.reasons} <= REASON_KINDS
        assert all(r.text.strip() for r in outcome.reasons)


def test_reason_kinds_are_never_repeated_within_one_decision() -> None:
    """Two reasons of the same kind would be one reason said twice, which
    spends the 2–3 budget without adding evidence."""
    for outcome in _all_outcomes():
        kinds = [r.kind for r in outcome.reasons]
        assert len(kinds) == len(set(kinds))


def test_adoption_history_is_not_emitted_in_p0() -> None:
    """Documented scope limit: the kind stays in the frozen vocabulary, but no
    loaded projection carries wear counts, so nothing here may claim one."""
    for outcome in _all_outcomes():
        assert all(r.kind != REASON_ADOPTION_HISTORY for r in outcome.reasons)


def _evidence_numbers(closet: ClosetEvidence, outfit: OutfitEvidence) -> set[str]:
    """Every count a reason is allowed to say out loud."""
    numbers = {
        str(closet.similar_item_count),
        str(len(closet.similar_items)),
        str(len(outfit.outfits)),
        str(len({i for o in outfit.outfits for i in o.item_ids})),
        str(outfit.available_item_count),
        str(len(outfit.alternatives)),
    }
    numbers.update(str(len(c.covered_by_item_ids)) for c in closet.coverage_scenarios)
    return numbers


@pytest.mark.parametrize("locale", ["zh-CN", "en-US"])
def test_every_number_in_a_reason_came_from_the_evidence(locale: str) -> None:
    """Acceptance 2 as a property of the data flow, not of the wording.

    A template that hard-codes "3 items" or derives a number from something we
    did not compute fails here for every fixture in the grid.
    """
    for parsed, closet, outfit in _grid():
        outcome = decide(parsed, closet, outfit, locale=locale)
        rendered = " ".join(
            [outcome.decision_text, outcome.closet_comparison.summary_text]
            + [r.text for r in outcome.reasons]
        )
        allowed = _evidence_numbers(closet, outfit)
        for number in re.findall(r"\d+", rendered):
            assert number in allowed, f"{number!r} is not in the evidence: {rendered}"


def _spoken_text(outcome: PurchaseDecisionOutcome) -> list[str]:
    """Everything the user reads EXCEPT `boundary_note`, whose whole job is to
    name the things this feature does not claim (V3_SPEC §5.4 layer 5)."""
    return [
        outcome.decision_text,
        outcome.closet_comparison.summary_text,
        *[r.text for r in outcome.reasons],
    ]


@pytest.mark.parametrize("locale", ["zh-CN", "en-US"])
def test_no_rendered_copy_claims_anything_about_size_fit_or_body(locale: str) -> None:
    for outcome in _all_outcomes(locale=locale):
        for text in _spoken_text(outcome):
            lowered = text.lower()
            for term in BANNED_CLAIM_TERMS:
                assert term not in lowered, f"{term!r} appears in {text!r}"


@pytest.mark.parametrize("locale", ["zh-CN", "en-US"])
def test_the_boundary_note_is_where_sizing_is_disclaimed(locale: str) -> None:
    """The deny-list above would be vacuous if the disclaimer were also silent
    about size — this asserts the copy actually says it."""
    note = decide(_candidate(), _closet(), _outfit(), locale=locale).boundary_note

    assert any(term in note.lower() for term in ("尺码", "size"))


def test_a_saturated_scan_is_reported_as_a_floor_not_a_total() -> None:
    """V3-005 hands us `same_category_scan_truncated` precisely so the copy
    cannot render "the 200 most recent" as a confident total."""
    truncated = decide(
        _candidate(),
        _closet(REDUNDANCY_HIGH, covers=5, count=200, truncated=True, top_reasons=_SAME_COLOUR),
        _outfit(),
    )
    complete = decide(
        _candidate(),
        _closet(REDUNDANCY_HIGH, covers=5, count=200, top_reasons=_SAME_COLOUR),
        _outfit(),
    )

    assert "至少 200" in truncated.closet_comparison.summary_text
    assert "至少 200" not in complete.closet_comparison.summary_text
    assert "200" in complete.closet_comparison.summary_text


def test_a_medium_band_names_the_attribute_that_actually_matched() -> None:
    material = decide(
        _candidate(),
        _closet(REDUNDANCY_MEDIUM, covers=1, top_reasons=_SAME_MATERIAL),
        _outfit(),
        locale="en-US",
    )
    redundancy = next(r for r in material.reasons if r.kind == REASON_REDUNDANCY)

    assert "material" in redundancy.text


def test_a_missing_slot_is_reported_as_an_extra_purchase() -> None:
    outcome = decide(
        _candidate(),
        _closet(REDUNDANCY_NONE),
        _outfit(OUTFIT_STATUS_INCOMPLETE, outfits=0, missing=(Category.SHOES.value,)),
    )

    assert outcome.reasons[0].kind == REASON_EXTRA_PURCHASE_NEEDED


def test_a_scene_claim_only_appears_when_the_evidence_carries_one() -> None:
    without = decide(_candidate(scene_tags=None), _closet(REDUNDANCY_NONE), _outfit())
    with_coverage = decide(
        _candidate(),
        _closet(REDUNDANCY_NONE, coverage=(("commute", 2, SCENE_SOURCE_WEAR_FEEDBACK),)),
        _outfit(),
    )

    assert all(r.kind != REASON_SCENE_COVERAGE for r in without.reasons)
    assert any(r.kind == REASON_SCENE_COVERAGE for r in with_coverage.reasons)


def test_the_leading_reason_matches_the_judgment() -> None:
    duplicate = decide(
        _candidate(),
        _closet(REDUNDANCY_HIGH, covers=2, top_reasons=_SAME_COLOUR),
        _outfit(),
    )
    fits_in = decide(_candidate(), _closet(REDUNDANCY_NONE), _outfit(outfits=2))

    assert duplicate.reasons[0].kind == REASON_REDUNDANCY
    assert fits_in.reasons[0].kind == REASON_COMPATIBILITY


# --- Acceptance 3: visual need is false when the evidence already settles it


def test_visual_need_is_none_without_a_grounded_outfit() -> None:
    """A preview is candidate + owned supporting items. With no outfit there is
    nothing honest to render, whatever the user asked about."""
    for concern in (CONCERN_NONE, CONCERN_VOLUME, CONCERN_COLOR, CONCERN_SCENE, CONCERN_REDUNDANCY):
        for status in (OUTFIT_STATUS_INCOMPLETE, OUTFIT_STATUS_NO_AVAILABLE_ITEMS):
            outcome = decide(
                _candidate(),
                _closet(REDUNDANCY_NONE),
                _outfit(status, outfits=0),
                concern=concern,
            )
            assert outcome.visual_need == DecisionVisualNeed.NONE.value


def test_visual_need_is_none_when_the_wardrobe_already_answered_it() -> None:
    """A composed outfit EXISTS here, so "nothing to render" is not what makes
    this `none` — owning the item already is."""
    evidence = _outfit(outfits=2, alternatives=1)
    outcome = decide(
        _candidate(),
        _closet(REDUNDANCY_HIGH, covers=2, top_reasons=_SAME_COLOUR),
        evidence,
    )

    assert evidence.outfits, "the 'nothing to render' branch must not be what makes this none"
    assert outcome.decision == PurchaseDecision.AVOID_DUPLICATE.value
    assert outcome.visual_need == DecisionVisualNeed.NONE.value


def test_a_settled_decision_and_a_visual_concern_differ_only_in_the_concern() -> None:
    """Minimal pair — the ONLY difference is what the user said they are unsure
    about, and that is enough to move the flag off `none`."""
    args = (_candidate(), _closet(REDUNDANCY_NONE), _outfit(outfits=2))

    assert decide(*args, concern=CONCERN_NONE).visual_need == DecisionVisualNeed.NONE.value
    assert decide(*args, concern=CONCERN_VOLUME).visual_need == DecisionVisualNeed.RECOMMENDED.value
    assert decide(*args, concern=CONCERN_COLOR).visual_need == DecisionVisualNeed.RECOMMENDED.value


def test_a_wardrobe_answerable_concern_does_not_ask_for_a_picture() -> None:
    """`redundancy` and `scene` are answered by the evidence we already have —
    treating every concern as visual would make acceptance 3 vacuous."""
    args = (_candidate(), _closet(REDUNDANCY_NONE), _outfit(outfits=2))

    assert decide(*args, concern=CONCERN_REDUNDANCY).visual_need == DecisionVisualNeed.NONE.value
    assert decide(*args, concern=CONCERN_SCENE).visual_need == DecisionVisualNeed.NONE.value


def test_a_volume_silhouette_is_the_only_difference_that_makes_it_helpful() -> None:
    """Minimal pair on the candidate: text can describe a straight coat, but
    "oversized" is a question about volume that only a picture answers."""
    plain = decide(_candidate(silhouette="straight"), _closet(REDUNDANCY_NONE), _outfit(outfits=2))
    voluminous = decide(_candidate(silhouette="oversized"), _closet(REDUNDANCY_NONE), _outfit(outfits=2))

    assert plain.visual_need == DecisionVisualNeed.NONE.value
    assert voluminous.visual_need == DecisionVisualNeed.HELPFUL.value


def test_an_unsettled_decision_offers_a_preview() -> None:
    """Uncertainty that a picture could reduce — the other side of acceptance 3."""
    outcome = decide(
        _candidate(),
        _closet(REDUNDANCY_NONE),
        _outfit(outfits=2, available=MIN_GROUNDING_ITEMS - 1),
    )

    assert outcome.uncertainty != DecisionUncertainty.LOW.value
    assert outcome.visual_need == DecisionVisualNeed.HELPFUL.value


def test_a_colour_question_is_recommended_rather_than_merely_helpful() -> None:
    outcome = decide(_candidate(), _closet(REDUNDANCY_NONE), _outfit(conflict=True))

    assert outcome.decision == PurchaseDecision.CONSIDER_ALTERNATE.value
    assert outcome.visual_need == DecisionVisualNeed.RECOMMENDED.value


def test_an_unparsed_candidate_never_asks_for_a_preview() -> None:
    outcome = decide(None, _closet(REDUNDANCY_UNKNOWN), _outfit(OUTFIT_STATUS_UNKNOWN_CANDIDATE, outfits=0))

    assert outcome.decision == PurchaseDecision.INSUFFICIENT_INFORMATION.value
    assert outcome.visual_need == DecisionVisualNeed.NONE.value


# --- Uncertainty ---------------------------------------------------------


def test_uncertainty_is_always_a_frozen_band() -> None:
    assert {o.uncertainty for o in _all_outcomes()} <= {u.value for u in DecisionUncertainty}


def test_a_truncated_scan_is_never_low_uncertainty() -> None:
    """If a window saturated, counts are floors and better pairings may exist
    outside what we looked at — that is exactly what `medium` is for."""
    for closet, outfit in (
        (_closet(REDUNDANCY_LOW, covers=5, count=200, truncated=True), _outfit(outfits=2)),
        (_closet(REDUNDANCY_LOW, covers=2), _outfit(outfits=2, truncated=True)),
    ):
        assert decide(_candidate(), closet, outfit).uncertainty != DecisionUncertainty.LOW.value


def test_an_almost_empty_wardrobe_is_never_low_uncertainty() -> None:
    """With two items on record, "nothing similar" says more about the wardrobe
    than about the purchase."""
    thin = decide(
        _candidate(), _closet(REDUNDANCY_NONE), _outfit(outfits=1, available=MIN_GROUNDING_ITEMS - 1)
    )
    grounded = decide(
        _candidate(),
        _closet(REDUNDANCY_NONE, coverage=(("commute", 2, SCENE_SOURCE_WEAR_FEEDBACK),)),
        _outfit(outfits=1, available=MIN_GROUNDING_ITEMS),
    )

    assert thin.uncertainty == DecisionUncertainty.MEDIUM.value
    assert grounded.uncertainty == DecisionUncertainty.LOW.value


def test_a_category_default_scene_claim_does_not_buy_confidence() -> None:
    """V3-005's weakest tier holds for every item of a category at once, so it
    cannot be the thing that makes a decision confident."""
    weak = decide(
        _candidate(),
        _closet(REDUNDANCY_NONE, coverage=(("commute", 2, SCENE_SOURCE_CATEGORY_DEFAULT),)),
        _outfit(outfits=1),
    )

    assert weak.uncertainty == DecisionUncertainty.MEDIUM.value


def test_an_unknown_candidate_is_high_uncertainty() -> None:
    outcome = decide(None, _closet(REDUNDANCY_UNKNOWN), _outfit(OUTFIT_STATUS_UNKNOWN_CANDIDATE, outfits=0))

    assert outcome.uncertainty == DecisionUncertainty.HIGH.value


# --- Next actions --------------------------------------------------------


def test_next_actions_are_a_non_empty_subset_of_the_frozen_four() -> None:
    for outcome in _all_outcomes():
        assert outcome.next_actions
        assert set(outcome.next_actions) <= NEXT_ACTIONS
        assert len(set(outcome.next_actions)) == len(outcome.next_actions)


def test_style_with_existing_is_offered_only_when_an_owned_look_exists() -> None:
    """Otherwise the action opens a screen with nothing on it."""
    with_alternative = decide(
        _candidate(),
        _closet(REDUNDANCY_HIGH, covers=2, top_reasons=_SAME_COLOUR),
        _outfit(alternatives=1),
    )
    without_alternative = decide(
        _candidate(),
        _closet(REDUNDANCY_HIGH, covers=2, top_reasons=_SAME_COLOUR),
        _outfit(alternatives=0),
    )

    assert ACTION_STYLE_WITH_EXISTING in with_alternative.next_actions
    assert ACTION_STYLE_WITH_EXISTING not in without_alternative.next_actions


def test_a_worth_adding_decision_still_offers_the_owned_look_it_found() -> None:
    """The other side of the gate, and the one the grid does not reach: every
    alternatives-bearing fixture there also carries a colour clash, which routes
    to `consider_alternate`. Without this case the `worth_adding` branch of
    `_next_actions` would never execute in a test."""
    outcome = decide(
        _candidate(),
        _closet(REDUNDANCY_LOW, covers=1),
        _outfit(outfits=1, alternatives=1),
    )

    assert outcome.decision == PurchaseDecision.WORTH_ADDING.value
    assert outcome.next_actions == (
        ACTION_ADD_TO_CANDIDATES,
        ACTION_STYLE_WITH_EXISTING,
        ACTION_SKIP,
    )


def test_an_action_is_never_offered_without_an_outfit_behind_it() -> None:
    for _, closet, outfit in _grid():
        outcome = decide(_candidate(), closet, outfit)
        if not outfit.alternatives:
            assert ACTION_STYLE_WITH_EXISTING not in outcome.next_actions


def test_a_colour_recheck_is_offered_exactly_where_colour_is_the_question() -> None:
    clash = decide(_candidate(), _closet(REDUNDANCY_NONE), _outfit(conflict=True))
    clean = decide(_candidate(), _closet(REDUNDANCY_NONE), _outfit(outfits=2))

    assert ACTION_RECHECK_COLOR in clash.next_actions
    assert ACTION_RECHECK_COLOR not in clean.next_actions


def test_a_duplicate_does_not_lead_with_add_to_candidates() -> None:
    outcome = decide(
        _candidate(),
        _closet(REDUNDANCY_HIGH, covers=2, top_reasons=_SAME_COLOUR),
        _outfit(alternatives=1),
    )

    assert ACTION_ADD_TO_CANDIDATES not in outcome.next_actions
    assert outcome.next_actions[-1] == ACTION_SKIP


# --- Payload, replay and the no-false-precision guard --------------------


def _leaves(value: Any) -> list[Any]:
    if isinstance(value, dict):
        return [leaf for v in value.values() for leaf in _leaves(v)]
    if isinstance(value, list):
        return [leaf for v in value for leaf in _leaves(v)]
    return [value]


def test_the_payload_carries_no_numeric_score() -> None:
    """V3_SPEC §5.4's anti-goal, kept from V3-005: bands and counts, never a
    "suitability 87". Bools are checked BEFORE ints because
    `isinstance(True, int)` is True in Python."""
    for outcome in _all_outcomes():
        for leaf in _leaves(outcome.as_payload()):
            assert not isinstance(leaf, float)
            if isinstance(leaf, bool):
                continue
            if isinstance(leaf, int):
                assert leaf == DECISION_VERSION


def test_the_comparison_list_is_capped_but_the_count_is_not() -> None:
    """A 43-item wardrobe shows a handful and still reports the real total —
    reading a total off a display list understates redundancy, which is the
    buy-encouraging direction."""
    outcome = decide(
        _candidate(),
        _closet(REDUNDANCY_HIGH, covers=5, complements=10, count=43, top_reasons=_SAME_COLOUR),
        _outfit(),
    )

    assert len(outcome.closet_comparison.items) == MAX_COMPARISON_ITEMS
    assert "43" in outcome.closet_comparison.summary_text


def test_covering_items_are_shown_before_complementary_ones() -> None:
    """V3-005 ranked them; slicing must not reorder what the user sees."""
    outcome = decide(_candidate(), _closet(REDUNDANCY_LOW, covers=2, complements=4), _outfit())
    relations = [i.relation for i in outcome.closet_comparison.items]

    assert relations == [
        RELATION_COVERS,
        RELATION_COVERS,
        RELATION_COMPLEMENTS,
        RELATION_COMPLEMENTS,
        RELATION_COMPLEMENTS,
    ]


def test_a_decision_replays_byte_identically_from_its_payload() -> None:
    for outcome in _all_outcomes():
        payload = outcome.as_payload()

        assert replay_decision(payload).as_payload() == payload


def test_replay_refuses_an_unreadable_version() -> None:
    payload = decide(_candidate(), _closet(), _outfit()).as_payload()
    payload["decision_version"] = DECISION_VERSION + 1

    with pytest.raises(ValueError, match="unsupported decision_version"):
        replay_decision(payload)


# --- Locale and input tolerance ------------------------------------------


def test_both_locales_define_exactly_the_same_template_keys() -> None:
    """A key present in one language and missing in the other is a KeyError in
    production for exactly one group of users, and nothing else would catch it:
    the grid below only renders the keys the current rules happen to reach."""
    zh, en = rules._TEMPLATES["zh-CN"], rules._TEMPLATES["en-US"]

    assert set(zh) == set(en)
    assert set(rules._SCENE_LABELS["zh-CN"]) == set(rules._SCENE_LABELS["en-US"])
    # Every scene V3-004 can emit must have a label in both.
    assert set(SCENE_TAGS) <= set(rules._SCENE_LABELS["zh-CN"])


def test_the_two_locales_do_not_render_the_same_sentences() -> None:
    zh = {r.text for o in _all_outcomes(locale="zh-CN") for r in o.reasons}
    en = {r.text for o in _all_outcomes(locale="en-US") for r in o.reasons}

    assert zh and en
    assert not zh & en


def test_an_unsupported_locale_falls_back_to_the_product_default() -> None:
    fallback = decide(_candidate(), _closet(), _outfit(), locale="fr-FR")
    default = decide(_candidate(), _closet(), _outfit())

    assert fallback.decision_text == default.decision_text


def test_an_unknown_concern_is_treated_as_none_rather_than_raising() -> None:
    """Validation is the router's job; a decision must not fail on a field that
    only grades the preview entry."""
    outcome = decide(_candidate(), _closet(REDUNDANCY_NONE), _outfit(outfits=2), concern="nonsense")

    assert outcome.visual_need == DecisionVisualNeed.NONE.value


def test_partial_and_missing_candidate_fields_never_raise() -> None:
    for parsed in (None, ParsedItem(), ParsedItem(category=Category.TOP.value), _candidate(colors=None)):
        assert decide(parsed, _closet(), _outfit()).decision in {d.value for d in PurchaseDecision}


# --- Contract alignment --------------------------------------------------


def test_the_response_literals_match_the_frozen_enums() -> None:
    """The wire literals live in the schema module and the CHECK constraints
    live in `app/common/enums.py`; drift between them would be a contract
    break that neither file can see on its own."""
    assert set(get_args(Decision)) == VOCABULARIES["decision"]
    assert set(get_args(Uncertainty)) == VOCABULARIES["uncertainty"]
    assert set(get_args(VisualNeed)) == VOCABULARIES["visual_need"]
    assert set(get_args(ReasonKind)) == REASON_KINDS
    assert set(get_args(NextAction)) == NEXT_ACTIONS


# --- Regressions for the codex review on BE#142 ------------------------------


def test_second_variation_clash_does_not_flip_a_clean_primary() -> None:
    """The composer ranks clashing pieces last, so a clash in a lower-ranked
    VARIATION is expected — it must not turn a clean primary look into
    `consider_alternate` with colour-warning copy."""
    clean_primary = _look()
    clashing_variation = _look(conflict=True)
    outfit = OutfitEvidence(
        status=OUTFIT_STATUS_COMPOSED,
        outfits=(clean_primary, clashing_variation),
        alternatives=(),
        missing_slots=(),
        missing_categories=(),
        available_item_count=12,
        scan_truncated=False,
    )

    outcome = rules.decide(ParsedItem(category="top", colors=["white"]), _closet(), outfit)

    assert outcome.decision == PurchaseDecision.WORTH_ADDING.value
    assert rules.ACTION_RECHECK_COLOR not in outcome.next_actions


def test_primary_clash_still_flags_consider_alternate() -> None:
    """The inverse guard: judging only the primary must not lose a REAL clash."""
    outfit = OutfitEvidence(
        status=OUTFIT_STATUS_COMPOSED,
        outfits=(_look(conflict=True), _look()),
        alternatives=(),
        missing_slots=(),
        missing_categories=(),
        available_item_count=12,
        scan_truncated=False,
    )

    outcome = rules.decide(ParsedItem(category="top", colors=["red"]), _closet(), outfit)

    assert outcome.decision == PurchaseDecision.CONSIDER_ALTERNATE.value


def test_low_band_copy_never_invents_an_attribute_comparison() -> None:
    """A category-only parse reaches REDUNDANCY_LOW because nothing could be
    compared — the copy must say the information was insufficient, not assert
    that colours and materials "are different"."""
    closet = _closet(REDUNDANCY_LOW, covers=2)

    unverified = rules.decide(ParsedItem(category="top"), closet, _outfit())
    red = next(r for r in unverified.reasons if r.kind == rules.REASON_REDUNDANCY)
    assert "不一样" not in red.text
    assert "different" not in red.text

    compared = rules.decide(
        ParsedItem(category="top", colors=["red"], material_hints=["wool"]), closet, _outfit()
    )
    red_compared = next(r for r in compared.reasons if r.kind == rules.REASON_REDUNDANCY)
    assert "不一样" in red_compared.text


def test_a_same_type_other_colour_does_not_say_try_another_colour() -> None:
    # Real run (2026-11-14): a black tee next to an owned white tee is a medium
    # band, not a colour clash — "check a different colour" was the wrong advice.
    similar = decide(
        _candidate(), _closet(REDUNDANCY_MEDIUM, covers=1, top_reasons=_SAME_MATERIAL), _outfit(), locale="en-US"
    )
    clash = decide(_candidate(), _closet(REDUNDANCY_NONE), _outfit(conflict=True), locale="en-US")

    assert similar.decision == clash.decision == PurchaseDecision.CONSIDER_ALTERNATE.value
    assert "different colour" not in similar.decision_text
    assert "another colour" in similar.decision_text
    assert ACTION_RECHECK_COLOR not in similar.next_actions
    assert "different colour" in clash.decision_text
