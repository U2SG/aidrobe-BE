"""V3-006 — grounded outfit compatibility evidence (pure layer).

No DB, no IO, no SQLAlchemy import, and — acceptance 3 — no image provider:
composing an outfit is a function of a `ParsedItem` (V3-004) plus the
`OwnedItemFacts` projections V3-005 already defines. The DB half lives in
`compose_outfit_evidence.py`. That split is the V3-004 / V3-005 arrangement,
and it is what lets the grounding fixtures run without a container.

**"Available" is narrower here than "owned" in V3-005, on purpose.** V3-005
asks *do I already own one of these*, so a coat in the laundry basket counts.
V3-006 asks *can this actually be worn as an outfit*, where it does not. The
status filter therefore lives in `compose_outfit_evidence.AVAILABLE_STATUSES`
and is narrower than `compute_similarity_evidence.OWNED_STATUSES`; the two
answer different questions and are deliberately not the same constant.

**Slots, not category pairs.** A realistic outfit covers a torso, legs and
feet, optionally with an outer layer — so the composition is expressed as slots
and the categories that can fill them, rather than as V3-005's
"complementary categories" adjacency. The difference is load-bearing in one
case: a dress fills torso *and* legs, so `dress + shoes` is a complete outfit
and a user who owns dresses must not be told they need to buy a top and a
bottom. `_eligible` encodes that as one rule (a category may fill a slot only
if every slot it occupies is still unfilled) instead of a table of exceptions.

**A missing slot is evidence, not a failure.** When the wardrobe cannot fill a
required slot, no outfit is invented: the gap is emitted as `missing_categories`,
which is what contract §5.3's frozen `extra_purchase_needed` reason kind is for
(V3_SPEC §5.4 layer 2, "是否需要额外购买其他单品"). That claim has to be earned,
which is why a colour clash *down-ranks* an item instead of excluding it: if
conflicting items were dropped from the pool, a user who owns shoes could be
told they need to buy shoes. Wrong in the expensive direction, and the same
class of error V3-005's round-1 scan window made in the buy-encouraging one.

**No score reaches the payload**, for the reason V3-005 spells out and
acceptance 3 of that feature froze: ranking weights order a list and stay
inside this module. Outfit evidence is item ids, slots, a scene with its
provenance, and a small reason vocabulary.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from app.application.purchase.candidate_normalizer import SCENE_TAGS, ParsedItem
from app.application.purchase.wardrobe_similarity import (
    SCENE_SOURCE_ITEM_TAGS,
    SCENE_SOURCE_WEAR_FEEDBACK,
    OwnedItemFacts,
)
from app.application.recommendations.colors import COLOR_CONFLICTS, NEUTRAL_COLORS
from app.common.enums import Category

# Bump when the emitted payload shape changes. Persisted inside V3-007's
# `evidence_snapshot`, so a replay knows which reader to use rather than
# rewriting historical rows (the `DECISION_SNAPSHOT_VERSION` convention).
OUTFIT_EVIDENCE_VERSION = 1

# --- Slots ---------------------------------------------------------------
SLOT_TORSO = "torso"
SLOT_LEGS = "legs"
SLOT_FEET = "feet"
SLOT_OUTER = "outer"

# Which slots a garment of this category occupies. A dress occupies two, which
# is the whole reason slots exist here — see the module docstring.
CATEGORY_SLOTS: dict[str, tuple[str, ...]] = {
    Category.TOP.value: (SLOT_TORSO,),
    Category.BOTTOM.value: (SLOT_LEGS,),
    Category.DRESS.value: (SLOT_TORSO, SLOT_LEGS),
    Category.OUTERWEAR.value: (SLOT_OUTER,),
    Category.SHOES.value: (SLOT_FEET,),
}

# Categories that can fill a slot, in preference order. `top` before `dress`
# for the torso: a separate top leaves the legs slot open to the wardrobe's
# ranking, whereas a dress decides both at once.
SLOT_CATEGORIES: dict[str, tuple[str, ...]] = {
    SLOT_TORSO: (Category.TOP.value, Category.DRESS.value),
    SLOT_LEGS: (Category.BOTTOM.value,),
    SLOT_FEET: (Category.SHOES.value,),
    SLOT_OUTER: (Category.OUTERWEAR.value,),
}

# An outfit is complete when torso, legs and feet are covered. The outer layer
# is genuinely optional — refusing to call a t-shirt-and-jeans outfit complete
# because there is no coat would manufacture a purchase need.
REQUIRED_SLOTS: tuple[str, ...] = (SLOT_TORSO, SLOT_LEGS, SLOT_FEET)
# Fill order. Torso first because it is the slot a dress can collapse.
FILL_ORDER: tuple[str, ...] = (SLOT_TORSO, SLOT_LEGS, SLOT_FEET, SLOT_OUTER)

# --- Frozen output vocabularies ------------------------------------------
# Why this set of supporting items reads as a real outfit. Deliberately small
# and categorical: V3-007 writes the sentence, this says what it may cite.
REASON_COMPLETE_LOOK = "complete_look"
REASON_SHARED_SCENE = "shared_scene"
REASON_NEUTRAL_ANCHOR = "neutral_color_anchor"
# Emitted when the best available pick still clashes with the candidate. Kept
# rather than suppressed: the outfit is the honest best the wardrobe can do,
# and hiding the clash would be the recommendation quietly overselling itself.
REASON_COLOR_CONFLICT = "color_conflict"

# How an outfit is grounded, or why it is not. V3-007 maps these onto decision
# states, so `no_available_items` (nothing to wear) and `unknown_candidate`
# (nothing to compose *with*) stay distinct the way V3-005's `none`/`unknown` do.
OUTFIT_STATUS_COMPOSED = "composed"
OUTFIT_STATUS_INCOMPLETE = "incomplete"
OUTFIT_STATUS_NO_AVAILABLE_ITEMS = "no_available_items"
OUTFIT_STATUS_UNKNOWN_CANDIDATE = "unknown_candidate"

# Caps. V3_SPEC §5.4 warns against a default "try another one" treadmill, so the
# engine offers a primary look plus one variation — not a browsable grid.
MAX_OUTFITS = 2
MAX_ALTERNATIVES = 2

# Ranking weights — internal only, never emitted (see the module docstring).
_WEIGHT_WEAR_FEEDBACK_SCENE = 4
_WEIGHT_ITEM_TAG_SCENE = 2
_WEIGHT_NEUTRAL = 1
_PENALTY_COLOR_CONFLICT = -4


@dataclass(frozen=True)
class OutfitSlot:
    """One filled slot: which owned item covers it, and as what."""

    slot: str
    item_id: uuid.UUID
    category: str

    def as_payload(self) -> dict[str, Any]:
        return {"slot": self.slot, "item_id": str(self.item_id), "category": self.category}

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> OutfitSlot:
        return cls(
            slot=str(payload["slot"]),
            item_id=uuid.UUID(str(payload["item_id"])),
            category=str(payload["category"]),
        )


@dataclass(frozen=True)
class GroundedOutfit:
    """One realistic look: the candidate (or an owned anchor) plus real items.

    `anchor_item_id` is None for a candidate-led outfit — the candidate is not
    owned and has no wardrobe id, so it cannot appear in `slots`. An alternative
    is anchored on an owned item instead, and that item DOES appear in `slots`,
    which is what makes `item_ids` an "only things you already own" list.
    """

    slots: tuple[OutfitSlot, ...]
    scene: str | None = None
    scene_source: str | None = None
    match_reasons: tuple[str, ...] = ()
    anchor_item_id: uuid.UUID | None = None

    @property
    def item_ids(self) -> tuple[uuid.UUID, ...]:
        """Every wardrobe item this outfit asks the user to wear."""
        return tuple(s.item_id for s in self.slots)

    def as_payload(self) -> dict[str, Any]:
        # No `image_url`: a signed URL has a 30-minute TTL and must be
        # materialized at read time (V3-008), never frozen into a snapshot.
        return {
            "anchor_item_id": str(self.anchor_item_id) if self.anchor_item_id else None,
            "slots": [s.as_payload() for s in self.slots],
            "item_ids": [str(i) for i in self.item_ids],
            "scene": self.scene,
            "scene_source": self.scene_source,
            "match_reasons": list(self.match_reasons),
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> GroundedOutfit:
        anchor = payload.get("anchor_item_id")
        return cls(
            slots=tuple(OutfitSlot.from_payload(s) for s in payload.get("slots", [])),
            scene=payload.get("scene"),
            scene_source=payload.get("scene_source"),
            match_reasons=tuple(payload.get("match_reasons", [])),
            anchor_item_id=uuid.UUID(str(anchor)) if anchor else None,
        )


@dataclass(frozen=True)
class OutfitEvidence:
    """What V3-007 freezes into `evidence_snapshot` and reasons from."""

    status: str
    outfits: tuple[GroundedOutfit, ...] = ()
    # Acceptance 2 — outfits built around an owned item the candidate would
    # duplicate, i.e. "you can already make this look".
    alternatives: tuple[GroundedOutfit, ...] = ()
    # Required slots the wardrobe cannot fill, and the categories that would
    # fill them. `missing_categories` is what contract §5.3's
    # `extra_purchase_needed` reason cites.
    missing_slots: tuple[str, ...] = ()
    missing_categories: tuple[str, ...] = ()
    available_item_count: int = 0
    # True when a per-category scan window saturated, so the pool this was
    # composed from is the most recent slice rather than the whole wardrobe.
    # Describes **V3-006's own available-item scan** — not V3-005's owned-item
    # window, which is a different filter over a different bound. V3-005's
    # `same_category_scan_truncated` reaches the caller separately and means
    # "the redundancy count is a floor"; this one means "there may be more
    # alternatives and better pairings than the ones listed".
    scan_truncated: bool = False
    evidence_version: int = OUTFIT_EVIDENCE_VERSION

    def as_payload(self) -> dict[str, Any]:
        return {
            "evidence_version": self.evidence_version,
            "status": self.status,
            "outfits": [o.as_payload() for o in self.outfits],
            "alternatives": [a.as_payload() for a in self.alternatives],
            "missing_slots": list(self.missing_slots),
            "missing_categories": list(self.missing_categories),
            "available_item_count": self.available_item_count,
            "scan_truncated": self.scan_truncated,
        }


def replay_outfit_evidence(payload: dict[str, Any]) -> OutfitEvidence:
    """Rebuild the evidence from a persisted snapshot — acceptance 4.

    The wardrobe is never consulted: everything the outfit asserts is inside the
    payload, so an explanation keeps reading the same way after the items it
    named were renamed, worn out or deleted. `as_payload()` after a replay is
    byte-identical to the payload that went in, which is the property the
    snapshot-replay test pins.

    An unknown `evidence_version` raises rather than guessing: a newer shape
    cannot be read correctly by an older reader, and silently returning a
    partially-understood snapshot would put a wrong explanation in front of a
    user. V3-003's versioning exists precisely so old rows are never rewritten.
    """
    version = payload.get("evidence_version")
    if version != OUTFIT_EVIDENCE_VERSION:
        raise ValueError(f"unsupported outfit evidence_version: {version!r}")
    return OutfitEvidence(
        status=str(payload["status"]),
        outfits=tuple(GroundedOutfit.from_payload(o) for o in payload.get("outfits", [])),
        alternatives=tuple(GroundedOutfit.from_payload(a) for a in payload.get("alternatives", [])),
        missing_slots=tuple(payload.get("missing_slots", [])),
        missing_categories=tuple(payload.get("missing_categories", [])),
        available_item_count=int(payload.get("available_item_count", 0)),
        scan_truncated=bool(payload.get("scan_truncated", False)),
        evidence_version=OUTFIT_EVIDENCE_VERSION,
    )


def unusable_candidate_evidence() -> OutfitEvidence:
    """The honest answer when V3-004 never parsed a category.

    Distinct from an empty wardrobe: this says we could not compose *anything*
    because we do not know what the candidate is, which is how V3-007 reaches
    `insufficient_information` instead of a confident "no outfits".
    """
    return OutfitEvidence(status=OUTFIT_STATUS_UNKNOWN_CANDIDATE)


def _candidate_colors(parsed: ParsedItem) -> set[str]:
    return {c.strip().lower() for c in (parsed.colors or []) if isinstance(c, str) and c.strip()}


def _candidate_scenes(parsed: ParsedItem) -> tuple[str, ...]:
    """Candidate scenes, de-duplicated, source order preserved."""
    seen: list[str] = []
    for scene in parsed.scene_tags or []:
        normalized = scene.strip().lower() if isinstance(scene, str) else ""
        if normalized and normalized not in seen:
            seen.append(normalized)
    return tuple(seen)


def _conflicts_with(facts: OwnedItemFacts, colors: set[str]) -> bool:
    own = (facts.color_main or "").strip().lower()
    return bool(own) and any(frozenset((own, other)) in COLOR_CONFLICTS for other in colors)


def _scene_source(scene: str, facts: OwnedItemFacts) -> str | None:
    """Evidence that this owned item is actually worn for `scene`, or None.

    Only the two *observed* tiers of V3-005's cascade. V3-005's third tier
    ("same category, therefore plausibly the same scene") is deliberately not
    used here: it holds for every item of a category at once, so it would let
    every outfit claim every scene, which is not grounding.
    """
    if scene in facts.wear_scenes:
        return SCENE_SOURCE_WEAR_FEEDBACK
    if scene in facts.style_tags:
        return SCENE_SOURCE_ITEM_TAGS
    return None


def _scene_weight(facts: OwnedItemFacts, scenes: tuple[str, ...]) -> int:
    best = 0
    for scene in scenes:
        source = _scene_source(scene, facts)
        if source == SCENE_SOURCE_WEAR_FEEDBACK:
            return _WEIGHT_WEAR_FEEDBACK_SCENE
        if source == SCENE_SOURCE_ITEM_TAGS:
            best = max(best, _WEIGHT_ITEM_TAG_SCENE)
    return best


def _pick_score(facts: OwnedItemFacts, parsed: ParsedItem) -> int:
    """Internal desirability weight. Never emitted — see the module docstring."""
    colors = _candidate_colors(parsed)
    score = _scene_weight(facts, _candidate_scenes(parsed))
    if (facts.color_main or "").strip().lower() in NEUTRAL_COLORS:
        score += _WEIGHT_NEUTRAL
    if _conflicts_with(facts, colors):
        score += _PENALTY_COLOR_CONFLICT
    return score


def _eligible(facts: OwnedItemFacts, slot: str, satisfied: frozenset[str]) -> bool:
    """May this garment fill `slot` given what the outfit already covers?

    One rule instead of a table of exceptions: a category may fill a slot only
    when *every* slot it occupies is still open. That is what stops a dress
    being suggested as the top half of an outfit whose legs slot is already the
    candidate's, and what lets `dress + shoes` count as complete.
    """
    slots = CATEGORY_SLOTS.get(facts.category, ())
    return slot in slots and not (set(slots) & satisfied)


def rank_for_slot(
    parsed: ParsedItem,
    facts: list[OwnedItemFacts],
    slot: str,
    satisfied: frozenset[str],
) -> list[OwnedItemFacts]:
    """Candidates for one slot, most wearable-with-the-candidate first.

    A stable sort on the negated weight only: the incoming order is the DB
    layer's `created_at DESC, id`, so equal-weight items keep recency order and
    the result is reproducible run to run. Same contract as V3-005's
    `build_evidence` — input order is authoritative for ties.
    """
    eligible = [f for f in facts if _eligible(f, slot, satisfied)]
    return sorted(eligible, key=lambda f: -_pick_score(f, parsed))


def _outfit_scene(
    picks: list[OwnedItemFacts],
    scenes: tuple[str, ...],
) -> tuple[str | None, str | None]:
    """The strongest scene this set of items has real evidence for.

    Candidate scene order decides ties, so a candidate tagged
    `["commute", "travel"]` reports `commute` when both are equally evidenced.
    """
    best: tuple[str, str] | None = None
    for scene in scenes:
        for facts in picks:
            source = _scene_source(scene, facts)
            if source == SCENE_SOURCE_WEAR_FEEDBACK:
                return scene, source
            if source == SCENE_SOURCE_ITEM_TAGS and best is None:
                best = (scene, source)
    return best if best is not None else (None, None)


def _compose_one(
    parsed: ParsedItem,
    pool: list[OwnedItemFacts],
    *,
    rank: int,
    prefilled: frozenset[str],
    anchor: OwnedItemFacts | None,
) -> tuple[GroundedOutfit | None, tuple[str, ...]]:
    """Fill the open slots once. Returns the outfit and the required slots it
    could not fill.

    `rank` selects the rank-th choice per slot so the second outfit is a real
    variation rather than a copy; a slot with fewer options falls back to its
    best item, and the caller drops the result if it duplicates an earlier one.
    """
    satisfied = set(prefilled)
    used: set[uuid.UUID] = set()
    slots: list[OutfitSlot] = []
    picks: list[OwnedItemFacts] = []
    missing: list[str] = []

    if anchor is not None:
        satisfied |= set(CATEGORY_SLOTS.get(anchor.category, ()))
        used.add(anchor.item_id)
        picks.append(anchor)
        for slot in CATEGORY_SLOTS.get(anchor.category, ()):
            slots.append(OutfitSlot(slot=slot, item_id=anchor.item_id, category=anchor.category))

    for slot in FILL_ORDER:
        if slot in satisfied:
            continue
        ranked = [f for f in rank_for_slot(parsed, pool, slot, frozenset(satisfied)) if f.item_id not in used]
        if not ranked:
            if slot in REQUIRED_SLOTS:
                missing.append(slot)
            continue
        chosen = ranked[rank] if rank < len(ranked) else ranked[0]
        used.add(chosen.item_id)
        picks.append(chosen)
        satisfied |= set(CATEGORY_SLOTS.get(chosen.category, ()))
        for filled in CATEGORY_SLOTS.get(chosen.category, ()):
            slots.append(OutfitSlot(slot=filled, item_id=chosen.item_id, category=chosen.category))

    if missing or not slots:
        return None, tuple(missing)

    scene, scene_source = _outfit_scene(picks, _candidate_scenes(parsed))
    reasons = [REASON_COMPLETE_LOOK]
    if scene is not None:
        reasons.append(REASON_SHARED_SCENE)
    if any((f.color_main or "").strip().lower() in NEUTRAL_COLORS for f in picks):
        reasons.append(REASON_NEUTRAL_ANCHOR)
    colors = _candidate_colors(parsed)
    if any(_conflicts_with(f, colors) for f in picks):
        reasons.append(REASON_COLOR_CONFLICT)

    return (
        GroundedOutfit(
            slots=tuple(slots),
            scene=scene,
            scene_source=scene_source,
            match_reasons=tuple(reasons),
            anchor_item_id=anchor.item_id if anchor is not None else None,
        ),
        (),
    )


def _distinct(outfits: list[GroundedOutfit]) -> tuple[GroundedOutfit, ...]:
    """Drop repeats — a wardrobe with one bottom yields the same outfit twice."""
    seen: set[frozenset[uuid.UUID]] = set()
    unique: list[GroundedOutfit] = []
    for outfit in outfits:
        key = frozenset(outfit.item_ids)
        if key in seen:
            continue
        seen.add(key)
        unique.append(outfit)
    return tuple(unique)


def build_outfit_evidence(
    parsed: ParsedItem | None,
    available: list[OwnedItemFacts],
    *,
    similar_item_ids: tuple[uuid.UUID, ...] = (),
    scan_truncated: bool = False,
) -> OutfitEvidence:
    """Compose grounded outfits from items the user owns and can wear today.

    `available` must already be filtered to available items by the caller — the
    DB layer does it in SQL so the window cannot be filled up by unwearable
    rows. Every id emitted here comes out of that list, which is what makes
    acceptance 1 a property of the data flow rather than of a promise.

    `similar_item_ids` are V3-005's `covers` items, in its ranking order. They
    seed the alternatives, and a non-empty list is exactly the condition
    "purchase is redundant" (V3-005 bands `none` when it finds no covering
    item), so acceptance 2 needs no separate redundancy parameter. Ids missing
    from `available` are skipped: a similar coat that is in the laundry is
    correctly *owned* evidence for V3-005 and an unwearable suggestion here.

    Never raises on partial input: an unparsed candidate degrades to
    `unusable_candidate_evidence()` so a failed V3-004 parse cannot break the
    decision flow.
    """
    if parsed is None or not parsed.is_usable:
        return unusable_candidate_evidence()

    category = (parsed.category or "").strip().lower()
    if category not in CATEGORY_SLOTS:
        # A category outside the outfit vocabulary occupies no slot, so every
        # slot would read as open and the composer would happily return a
        # complete look the candidate is not even part of — a "grounded outfit"
        # that grounds nothing. `parsed.category` is normally a `Category`
        # member, but V3-004 passes a provider's reading straight through, so
        # this is reachable. The honest answer is the same as an unparsed
        # candidate: we cannot place it.
        return unusable_candidate_evidence()
    if not available:
        return OutfitEvidence(status=OUTFIT_STATUS_NO_AVAILABLE_ITEMS, scan_truncated=scan_truncated)

    prefilled = frozenset(CATEGORY_SLOTS[category])

    composed: list[GroundedOutfit] = []
    missing: tuple[str, ...] = ()
    for rank in range(MAX_OUTFITS):
        outfit, gaps = _compose_one(parsed, available, rank=rank, prefilled=prefilled, anchor=None)
        if outfit is None:
            # Every rank hits the same empty slot — the wardrobe lacks the
            # category, it is not a ranking accident.
            missing = gaps
            break
        composed.append(outfit)

    by_id = {f.item_id: f for f in available}
    alternatives: list[GroundedOutfit] = []
    for item_id in similar_item_ids:
        if len(alternatives) >= MAX_ALTERNATIVES:
            break
        anchor = by_id.get(item_id)
        if anchor is None:
            continue
        alternative, _ = _compose_one(parsed, available, rank=0, prefilled=frozenset(), anchor=anchor)
        if alternative is not None:
            alternatives.append(alternative)

    outfits = _distinct(composed)
    status = OUTFIT_STATUS_COMPOSED if outfits else OUTFIT_STATUS_INCOMPLETE
    return OutfitEvidence(
        status=status,
        outfits=outfits,
        alternatives=_distinct(alternatives),
        missing_slots=missing,
        missing_categories=tuple(SLOT_CATEGORIES[slot][0] for slot in missing),
        available_item_count=len(available),
        scan_truncated=scan_truncated,
    )


_ALL_MATCH_REASONS = frozenset(
    {REASON_COMPLETE_LOOK, REASON_SHARED_SCENE, REASON_NEUTRAL_ANCHOR, REASON_COLOR_CONFLICT}
)
_ALL_STATUSES = frozenset(
    {
        OUTFIT_STATUS_COMPOSED,
        OUTFIT_STATUS_INCOMPLETE,
        OUTFIT_STATUS_NO_AVAILABLE_ITEMS,
        OUTFIT_STATUS_UNKNOWN_CANDIDATE,
    }
)
# Re-exported for the no-numeric-score guard, which walks a rendered payload
# and asserts every leaf is one of these literals, an id, or a count.
VOCABULARIES: dict[str, frozenset[str]] = {
    "match_reasons": _ALL_MATCH_REASONS,
    "status": _ALL_STATUSES,
    "slot": frozenset(FILL_ORDER),
    "category": frozenset(CATEGORY_SLOTS),
    "scene_source": frozenset({SCENE_SOURCE_WEAR_FEEDBACK, SCENE_SOURCE_ITEM_TAGS}),
    # A scene is echoed from the candidate, whose tags V3-004 already restricts
    # to this whitelist — so the guard doubles as a check that no free-text
    # style tag can leak out through the scene field.
    "scene": frozenset(SCENE_TAGS),
}

__all__ = [
    "CATEGORY_SLOTS",
    "FILL_ORDER",
    "MAX_ALTERNATIVES",
    "MAX_OUTFITS",
    "OUTFIT_EVIDENCE_VERSION",
    "OUTFIT_STATUS_COMPOSED",
    "OUTFIT_STATUS_INCOMPLETE",
    "OUTFIT_STATUS_NO_AVAILABLE_ITEMS",
    "OUTFIT_STATUS_UNKNOWN_CANDIDATE",
    "REASON_COLOR_CONFLICT",
    "REASON_COMPLETE_LOOK",
    "REASON_NEUTRAL_ANCHOR",
    "REASON_SHARED_SCENE",
    "REQUIRED_SLOTS",
    "SLOT_CATEGORIES",
    "SLOT_FEET",
    "SLOT_LEGS",
    "SLOT_OUTER",
    "SLOT_TORSO",
    "VOCABULARIES",
    "GroundedOutfit",
    "OutfitEvidence",
    "OutfitSlot",
    "build_outfit_evidence",
    "rank_for_slot",
    "replay_outfit_evidence",
    "unusable_candidate_evidence",
]
