"""V3-007 — the bounded purchase decision (pure layer).

No DB, no IO, no SQLAlchemy import: a decision is a function of V3-004's
`ParsedItem`, V3-005's `ClosetEvidence`, V3-006's `OutfitEvidence` and the
user's declared `concern`. `decide_purchase.py` does the queries and the
persistence. That split is the arrangement V3-004/005/006 established, and it
is what lets the whole decision matrix be tested without a container.

**Rules, not an LLM.** V3_SPEC §10.1 assigns 购买决策 to "推荐规则 + 用户记忆 +
可解释排序" and §10.2 limits the LLM to turning language into structure and
polishing text on top of structured evidence. M17's `sense_check._decide` is
the in-repo precedent for a rule-based verdict. So every field below is derived
from evidence that was already computed and is about to be frozen into
`wardrobe_decisions.evidence_snapshot`; nothing here is generated.

**Why the copy is a template table.** Contract §5.3 freezes `decision_text` and
`reasons[].text` as plain strings and says the client never synthesizes them.
Templates keep every sentence auditable: a reason can only interpolate numbers
and labels that came out of the evidence, so "grounded" is a property of the
data flow rather than a promise (`test_v3_purchase_decision_rules` asserts
every digit in every rendered reason appears in the evidence payload).

**No fit, size or body claims** (acceptance 2). The vocabulary tables below
contain no sizing word by construction, and `BANNED_CLAIM_TERMS` is the
explicit deny-list the tests walk the rendered copy against. The single place
sizing is mentioned is `boundary_note`, whose whole job is to disclaim it
(V3_SPEC §5.4 layer 5).

**`adoption_history` is deliberately not emitted.** The kind stays in the
frozen §5.3 vocabulary, but P0 emits four of five: no projection loaded by
V3-005 or V3-006 carries wear counts, and adding one would mean editing
`OwnedItemFacts` and both scan statements to answer a question no V3-007
acceptance criterion asks. Recorded as a carried risk instead of a weak signal.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from app.application.purchase.candidate_normalizer import ParsedItem
from app.application.purchase.outfit_composition import (
    OUTFIT_STATUS_COMPOSED,
    OUTFIT_STATUS_NO_AVAILABLE_ITEMS,
    OUTFIT_STATUS_UNKNOWN_CANDIDATE,
    REASON_COLOR_CONFLICT,
    OutfitEvidence,
)
from app.application.purchase.wardrobe_similarity import (
    REASON_SHARED_MATERIAL,
    REASON_SHARED_SCENE,
    REDUNDANCY_HIGH,
    REDUNDANCY_MEDIUM,
    REDUNDANCY_NONE,
    REDUNDANCY_UNKNOWN,
    RELATION_COVERS,
    SCENE_SOURCE_CATEGORY_DEFAULT,
    ClosetEvidence,
)
from app.common.enums import (
    DecisionUncertainty,
    DecisionVisualNeed,
    PurchaseDecision,
)
from app.common.item_labels import CATEGORY_LABELS
from app.common.locale import DEFAULT_LOCALE, resolve_locale

# Bump when the persisted decision payload shape changes. A replay refuses an
# unknown version rather than guessing, exactly like V3-006's
# `replay_outfit_evidence` (and for the same reason: an older reader cannot
# render a newer shape correctly, and a half-understood explanation in front of
# a user is worse than an error).
DECISION_VERSION = 1

# --- Frozen literals (docs/v3-p0-contract.md §5.3) -----------------------
# Request `concern`. Kept as module constants rather than a `StrEnum` in
# `app/common/enums.py` because nothing here is persisted as a column with a
# CHECK constraint to stay in sync with — unlike `decision` / `uncertainty` /
# `visual_need`, which are columns and therefore live in the enums module.
CONCERN_VOLUME = "volume"
CONCERN_COLOR = "color"
CONCERN_REDUNDANCY = "redundancy"
CONCERN_SCENE = "scene"
CONCERN_NONE = "none"
CONCERNS: frozenset[str] = frozenset(
    {CONCERN_VOLUME, CONCERN_COLOR, CONCERN_REDUNDANCY, CONCERN_SCENE, CONCERN_NONE}
)

# `reasons[].kind`.
REASON_REDUNDANCY = "redundancy"
REASON_COMPATIBILITY = "compatibility"
REASON_SCENE_COVERAGE = "scene_coverage"
REASON_EXTRA_PURCHASE_NEEDED = "extra_purchase_needed"
REASON_ADOPTION_HISTORY = "adoption_history"  # frozen, not emitted in P0 — see docstring
REASON_KINDS: frozenset[str] = frozenset(
    {
        REASON_REDUNDANCY,
        REASON_COMPATIBILITY,
        REASON_SCENE_COVERAGE,
        REASON_EXTRA_PURCHASE_NEEDED,
        REASON_ADOPTION_HISTORY,
    }
)

# `next_actions` (V3_SPEC §5.4 layer 6).
ACTION_ADD_TO_CANDIDATES = "add_to_candidates"
ACTION_SKIP = "skip"
ACTION_RECHECK_COLOR = "recheck_color"
ACTION_STYLE_WITH_EXISTING = "style_with_existing"
NEXT_ACTIONS: frozenset[str] = frozenset(
    {ACTION_ADD_TO_CANDIDATES, ACTION_SKIP, ACTION_RECHECK_COLOR, ACTION_STYLE_WITH_EXISTING}
)

# Contract §5.3: "2–3, ordered by importance". Held unconditionally, including
# for `insufficient_information` — "we could not tell whether it duplicates
# anything" and "we could not tell whether it can be worn" are two honest
# statements about the evidence, not filler.
MIN_REASONS = 2
MAX_REASONS = 3

# How many owned items the comparison block shows (V3_SPEC §5.4 layer 3 is a
# handful, not a wardrobe dump). V3-005 already ordered `similar_items` with
# `covers` first, so slicing preserves its ranking authority.
MAX_COMPARISON_ITEMS = 5

# Below this the wardrobe cannot meaningfully contradict a candidate: with two
# items on record, "nothing similar" says more about the wardrobe than about
# the purchase. Used only to hold `uncertainty` off `low` — it never changes
# the decision itself.
MIN_GROUNDING_ITEMS = 5

# Silhouettes whose whole question is volume, which text cannot settle
# (`candidate_normalizer.SILHOUETTES` vocabulary). Drives `visual_need`
# upward — never the decision.
VOLUME_SILHOUETTES: frozenset[str] = frozenset({"oversized", "cropped", "wide_leg", "longline", "a_line"})

# Concerns a static preview can actually answer. `redundancy` and `scene` are
# answered by wardrobe evidence, so they must NOT pull `visual_need` up —
# that is acceptance 3 stated as data.
VISUAL_CONCERNS: frozenset[str] = frozenset({CONCERN_VOLUME, CONCERN_COLOR})

# Vocabulary that would assert something about the user's body or the garment's
# physical behaviour — claims this feature has no evidence for and V3_SPEC
# §5.4 layer 5 explicitly disclaims. The tests walk every rendered string
# except `boundary_note` (which exists to say we are NOT claiming these).
BANNED_CLAIM_TERMS: frozenset[str] = frozenset(
    {
        "尺码",
        "尺寸",
        "合身",
        "显瘦",
        "显胖",
        "身材",
        "身高",
        "体重",
        "腰围",
        "垂坠",
        "版型准",
        "穿上会",
        "size",
        "fits you",
        "true to size",
        "flattering",
        "slimming",
        "body type",
        "drape",
    }
)


@dataclass(frozen=True)
class DecisionReason:
    """One entry of contract §5.3 `reasons` — a frozen kind plus display copy."""

    kind: str
    text: str

    def as_payload(self) -> dict[str, Any]:
        return {"kind": self.kind, "text": self.text}

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> DecisionReason:
        return cls(kind=str(payload["kind"]), text=str(payload["text"]))


@dataclass(frozen=True)
class ComparisonItem:
    """One row of §5.3 `closet_comparison.similar_items`, minus `image_url`.

    The signed URL has a 30-minute TTL, so it is materialized at read time by
    the endpoint and never frozen into a snapshot — the same rule V3-005 and
    V3-006 apply to their payloads.
    """

    item_id: uuid.UUID
    relation: str

    def as_payload(self) -> dict[str, Any]:
        return {"item_id": str(self.item_id), "relation": self.relation}

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ComparisonItem:
        return cls(item_id=uuid.UUID(str(payload["item_id"])), relation=str(payload["relation"]))


@dataclass(frozen=True)
class ClosetComparison:
    """§5.3 `closet_comparison` — V3_SPEC §5.4 layer 3."""

    summary_text: str
    items: tuple[ComparisonItem, ...] = ()

    def as_payload(self) -> dict[str, Any]:
        return {
            "summary_text": self.summary_text,
            "similar_items": [i.as_payload() for i in self.items],
        }

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> ClosetComparison:
        return cls(
            summary_text=str(payload["summary_text"]),
            items=tuple(ComparisonItem.from_payload(i) for i in payload.get("similar_items", [])),
        )


@dataclass(frozen=True)
class PurchaseDecisionOutcome:
    """Everything §5.3 shows, minus what only the database knows (`id`,
    `candidate_id`, `created_at`) and minus the signed URLs."""

    decision: str
    decision_text: str
    reasons: tuple[DecisionReason, ...]
    closet_comparison: ClosetComparison
    uncertainty: str
    visual_need: str
    boundary_note: str
    next_actions: tuple[str, ...]
    decision_version: int = DECISION_VERSION

    def as_payload(self) -> dict[str, Any]:
        return {
            "decision_version": self.decision_version,
            "decision": self.decision,
            "decision_text": self.decision_text,
            "reasons": [r.as_payload() for r in self.reasons],
            "closet_comparison": self.closet_comparison.as_payload(),
            "uncertainty": self.uncertainty,
            "visual_need": self.visual_need,
            "boundary_note": self.boundary_note,
            "next_actions": list(self.next_actions),
        }


def replay_decision(payload: dict[str, Any]) -> PurchaseDecisionOutcome:
    """Rebuild the shown decision from its snapshot.

    Used by the idempotent repeat POST (contract §5.3: repeated calls return
    the existing decision) and by V3-008's `GET /v1/wardrobe-decisions/{id}`.
    Nothing is recomputed — the wardrobe may have changed since, and the whole
    point of the snapshot is that the explanation does not.
    """
    version = payload.get("decision_version")
    if version != DECISION_VERSION:
        raise ValueError(f"unsupported decision_version: {version!r}")
    return PurchaseDecisionOutcome(
        decision=str(payload["decision"]),
        decision_text=str(payload["decision_text"]),
        reasons=tuple(DecisionReason.from_payload(r) for r in payload.get("reasons", [])),
        closet_comparison=ClosetComparison.from_payload(payload["closet_comparison"]),
        uncertainty=str(payload["uncertainty"]),
        visual_need=str(payload["visual_need"]),
        boundary_note=str(payload["boundary_note"]),
        next_actions=tuple(payload.get("next_actions", [])),
        decision_version=DECISION_VERSION,
    )


# --- Display copy --------------------------------------------------------
# Server-rendered because §5.3 forbids the client to synthesize it, and
# bilingual because the frozen shape carries no i18n key, so a client has no
# way to localize a string we send in the wrong language. The locale is
# resolved once at decision time and the result is frozen into the snapshot —
# a decision keeps reading the way it read when it was made.

_SCENE_LABELS: dict[str, dict[str, str]] = {
    "zh-CN": {
        "commute": "通勤",
        "office": "办公",
        "formal": "正式场合",
        "interview": "面试",
        "wedding": "婚礼",
        "party": "聚会",
        "date": "约会",
        "travel": "旅行",
        "sport": "运动",
        "outdoor": "户外",
        "casual": "日常",
    },
    "en-US": {
        "commute": "commuting",
        "office": "the office",
        "formal": "formal occasions",
        "interview": "interviews",
        "wedding": "weddings",
        "party": "parties",
        "date": "dates",
        "travel": "travel",
        "sport": "sport",
        "outdoor": "the outdoors",
        "casual": "everyday wear",
    },
}

_TEMPLATES: dict[str, dict[str, str]] = {
    "zh-CN": {
        "decision.worth_adding": "值得加入。它能和你现在的衣橱组成可以直接穿的搭配。",
        "decision.consider_alternate": "可以考虑，但建议换个颜色再判断。",
        "decision.wait": "先等等。以现在的衣橱，它还穿不出完整的一套。",
        "decision.avoid_duplicate": "不建议重复购买。你已经有能做同样事的单品。",
        "decision.insufficient_information": "信息不足，先确认关键条件。商品信息没有解析出可比对的属性。",
        "boundary_note": "用于判断整体感觉；实际尺码、触感和精确垂坠仍需参考商品信息。",
        "summary.duplicate": "衣橱里已有 {count} 件同类的{category}，其中有一件颜色也相同。",
        "summary.similar": "衣橱里已有 {count} 件同类的{category}，属性上和它相近。",
        "summary.nothing_similar": "衣橱里没有同类的{category}，不属于重复购买。",
        "summary.no_items": "衣橱里还没有可以拿来对照的单品。",
        "summary.unknown": "没有识别出品类，无法和衣橱做对照。",
        "reason.redundancy.high": "你已经有 {count} 件同类的{category}，其中有颜色相同的一件。",
        "reason.redundancy.medium": "你已经有 {count} 件同类的{category}，{shared}也相近。",
        "reason.redundancy.low": "你有 {count} 件同类的{category}，但颜色和材质都不一样。",
        "reason.redundancy.low_unverified": "你有 {count} 件同类的{category}；商品信息不足，无法比较颜色和材质。",
        "reason.redundancy.none": "衣橱里没有同类的{category}，不属于重复购买。",
        "reason.redundancy.unknown": "没有解析出品类，无法判断它是否和现有衣物重复。",
        "reason.compatibility.composed": "它可以和你已有的 {items} 件单品组成 {outfits} 套完整搭配。",
        "reason.compatibility.color_conflict": "能搭起来，但和常搭的单品有明显撞色。",
        "reason.compatibility.incomplete": "用现在的衣橱凑不出一套完整的搭配。",
        "reason.compatibility.no_items": "衣橱里还没有可以和它搭配的单品。",
        "reason.compatibility.unknown": "没有解析出品类，无法确认它能不能和现有单品搭起来。",
        "reason.scene_coverage.covered": "{scene}场景你已经有 {count} 件可用的单品。",
        "reason.scene_coverage.uncovered": "{scene}场景衣橱里还没有对应的穿着记录。",
        "reason.extra_purchase_needed": "要穿上它，还需要另外添置{categories}。",
        "shared.material": "材质",
        "shared.scene": "适用场景",
        "shared.attributes": "属性",
        "count.floor": "至少 {count}",
        "join": "、",
    },
    "en-US": {
        "decision.worth_adding": "Worth adding. It makes a wearable outfit with what you already own.",
        "decision.consider_alternate": "Worth considering, but check a different colour first.",
        "decision.wait": "Hold off. Your wardrobe cannot complete an outfit around it yet.",
        "decision.avoid_duplicate": "Not worth duplicating. You already own something that does this job.",
        "decision.insufficient_information": (
            "Not enough information yet — the listing gave us no attributes to compare."
        ),
        "boundary_note": (
            "This is about the overall look; actual size, feel and drape still come from the product details."
        ),
        "summary.duplicate": "You already own {count} {category} in the same category, one in the same colour.",
        "summary.similar": "You already own {count} {category} with similar attributes.",
        "summary.nothing_similar": "You own no {category} in this category, so this is not a duplicate.",
        "summary.no_items": "There is nothing in your wardrobe to compare this against yet.",
        "summary.unknown": "No category was recognised, so there is nothing to compare against.",
        "reason.redundancy.high": "You already own {count} {category}, one of them in the same colour.",
        "reason.redundancy.medium": "You already own {count} {category}, with similar {shared}.",
        "reason.redundancy.low": "You own {count} {category}, but in different colours and materials.",
        "reason.redundancy.low_unverified": (
            "You own {count} {category}; the listing did not give enough detail "
            "to compare colours or materials."
        ),
        "reason.redundancy.none": "You own no {category} in this category, so this is not a duplicate.",
        "reason.redundancy.unknown": "No category was parsed, so we cannot tell whether it duplicates anything.",
        "reason.compatibility.composed": "It pairs with {items} items you own to make {outfits} complete outfits.",
        "reason.compatibility.color_conflict": "It works, but it clashes with the pieces it would be worn with.",
        "reason.compatibility.incomplete": "Your wardrobe cannot complete an outfit around it right now.",
        "reason.compatibility.no_items": "You have nothing available to wear with it yet.",
        "reason.compatibility.unknown": "No category was parsed, so we cannot tell what it would pair with.",
        "reason.scene_coverage.covered": "For {scene} you already have {count} items on record.",
        "reason.scene_coverage.uncovered": "For {scene} your wardrobe has no wear record yet.",
        "reason.extra_purchase_needed": "To wear it you would also need to buy {categories}.",
        "shared.material": "material",
        "shared.scene": "occasions",
        "shared.attributes": "attributes",
        "count.floor": "at least {count}",
        "join": ", ",
    },
}


def _text(locale: str, key: str, **params: Any) -> str:
    return _TEMPLATES[locale][key].format(**params)


def _category_label(locale: str, category: str | None) -> str:
    labels = CATEGORY_LABELS[locale]
    return labels.get((category or "").strip().lower(), labels["other"])


def _scene_label(locale: str, scene: str) -> str:
    return _SCENE_LABELS[locale].get(scene, scene)


def _count_text(locale: str, count: int, *, floor: bool) -> str:
    """A saturated scan window reports a floor, never a confident total —
    V3-005 hands us `same_category_scan_truncated` precisely so this copy
    cannot render 200-of-many as "200"."""
    return _text(locale, "count.floor", count=count) if floor else str(count)


# --- Decision matrix -----------------------------------------------------


def _is_unusable(closet: ClosetEvidence, outfit: OutfitEvidence) -> bool:
    """Both evidence modules degrade to their own "we could not look" state on
    an unparsed candidate, and they are checked together rather than trusting
    one: they read different fields, and disagreeing is itself a reason not to
    assert anything."""
    return closet.redundancy_level == REDUNDANCY_UNKNOWN or outfit.status == OUTFIT_STATUS_UNKNOWN_CANDIDATE


def _has_color_conflict(outfit: OutfitEvidence) -> bool:
    """Judge only the top-ranked outfit — the one the user is shown first.

    The composer deliberately ranks clashing pieces last, so a clash in a
    lower-ranked *variation* is expected and must not flip a clean primary
    look into `consider_alternate` (codex review, BE#142).
    """
    if not outfit.outfits:
        return False
    return REASON_COLOR_CONFLICT in outfit.outfits[0].match_reasons


def _decision_state(closet: ClosetEvidence, outfit: OutfitEvidence) -> str:
    """The five bounded states (acceptance 1), first match wins.

    Order is load-bearing: a duplicate stays a duplicate even when the wardrobe
    also cannot complete a look around the candidate, because "you already own
    one" is the answer the user needs either way — and `wait` would read as
    "buy more things first", which is the expensive direction to be wrong in.
    """
    if _is_unusable(closet, outfit):
        return PurchaseDecision.INSUFFICIENT_INFORMATION.value
    if closet.redundancy_level == REDUNDANCY_HIGH:
        return PurchaseDecision.AVOID_DUPLICATE.value
    if outfit.status != OUTFIT_STATUS_COMPOSED:
        # No complete look exists: either a required slot is empty (the
        # wardrobe would need another purchase first) or there is nothing
        # available at all. Both are "not yet", not "no".
        return PurchaseDecision.WAIT.value
    if _has_color_conflict(outfit) or closet.redundancy_level == REDUNDANCY_MEDIUM:
        return PurchaseDecision.CONSIDER_ALTERNATE.value
    return PurchaseDecision.WORTH_ADDING.value


def _observed_coverage(closet: ClosetEvidence) -> bool:
    """True when at least one scene claim rests on something the user actually
    did (a wear record or their own item tags) rather than on V3-005's weakest
    "same category, therefore plausibly the same scene" default."""
    return any(c.source != SCENE_SOURCE_CATEGORY_DEFAULT for c in closet.coverage_scenarios)


def _uncertainty(closet: ClosetEvidence, outfit: OutfitEvidence) -> str:
    """How much the evidence actually supports the judgment.

    Bands, not a score (V3_SPEC §5.4). Every branch names the specific thing
    that is missing, so an evaluator can check it against the payload.
    """
    if _is_unusable(closet, outfit):
        return DecisionUncertainty.HIGH.value
    if outfit.status == OUTFIT_STATUS_NO_AVAILABLE_ITEMS:
        # Nothing to compare against and nothing to compose with.
        return DecisionUncertainty.HIGH.value
    if closet.same_category_scan_truncated or outfit.scan_truncated:
        # A scan window saturated, so counts are floors and there may be
        # better pairings than the ones we looked at.
        return DecisionUncertainty.MEDIUM.value
    if outfit.status != OUTFIT_STATUS_COMPOSED:
        return DecisionUncertainty.MEDIUM.value
    if outfit.available_item_count < MIN_GROUNDING_ITEMS:
        return DecisionUncertainty.MEDIUM.value
    # Past here: a complete scan over a real wardrobe produced a wearable look,
    # so the remaining question is whether the *scene* claims are evidenced.
    if closet.coverage_scenarios and not _observed_coverage(closet):
        # Only V3-005's weakest tier answered, and that tier holds for every
        # item of a category at once — it cannot make a decision confident.
        return DecisionUncertainty.MEDIUM.value
    if closet.scenarios_without_evidence and not closet.coverage_scenarios:
        # The candidate names occasions the wardrobe has no record of at all.
        return DecisionUncertainty.MEDIUM.value
    return DecisionUncertainty.LOW.value


def _visual_need(
    decision: str,
    parsed: ParsedItem | None,
    outfit: OutfitEvidence,
    uncertainty: str,
    concern: str,
) -> str:
    """Acceptance 3 — `none` whenever text and wardrobe evidence already settle it.

    Read top to bottom, the guards are: there must be something real to render,
    the question must not already be answered, and then the *kind* of doubt
    that is left decides how prominent the entry is. A preview costs a paid
    provider call (V3-016/017), so a decision that a picture cannot change
    must not advertise one.
    """
    if not outfit.outfits:
        # A preview is candidate + owned supporting items (V3_SPEC §5.4 layer
        # 4). With no grounded outfit there is nothing honest to generate.
        return DecisionVisualNeed.NONE.value
    if decision in (
        PurchaseDecision.AVOID_DUPLICATE.value,
        PurchaseDecision.INSUFFICIENT_INFORMATION.value,
    ):
        # Settled by the wardrobe (you own one) or unanswerable (we do not know
        # what it is). Seeing it on a body changes neither.
        return DecisionVisualNeed.NONE.value
    if concern in VISUAL_CONCERNS:
        # The user said in their own words that the open question is visual.
        return DecisionVisualNeed.RECOMMENDED.value
    if decision == PurchaseDecision.CONSIDER_ALTERNATE.value:
        # The unresolved part IS how it looks next to what it would be worn with.
        return DecisionVisualNeed.RECOMMENDED.value
    if parsed is not None and (parsed.silhouette or "") in VOLUME_SILHOUETTES:
        return DecisionVisualNeed.HELPFUL.value
    if uncertainty != DecisionUncertainty.LOW.value:
        return DecisionVisualNeed.HELPFUL.value
    return DecisionVisualNeed.NONE.value


def _next_actions(decision: str, closet: ClosetEvidence, outfit: OutfitEvidence) -> tuple[str, ...]:
    """The frozen four, filtered to the ones that lead somewhere real.

    `style_with_existing` is offered only when V3-006 actually composed an
    owned-item alternative — offering it otherwise would open a screen with
    nothing on it. V3_SPEC §5.4 layer 6 also warns against an endless
    "try another" menu, so each state emits a short, ordered list.
    """
    actions: list[str] = []
    if decision == PurchaseDecision.AVOID_DUPLICATE.value:
        if outfit.alternatives:
            actions.append(ACTION_STYLE_WITH_EXISTING)
        actions.append(ACTION_SKIP)
    elif decision == PurchaseDecision.CONSIDER_ALTERNATE.value:
        actions.append(ACTION_RECHECK_COLOR)
        actions.append(ACTION_ADD_TO_CANDIDATES)
        if outfit.alternatives:
            actions.append(ACTION_STYLE_WITH_EXISTING)
    elif decision == PurchaseDecision.WAIT.value:
        actions.extend((ACTION_ADD_TO_CANDIDATES, ACTION_SKIP))
    elif decision == PurchaseDecision.INSUFFICIENT_INFORMATION.value:
        actions.extend((ACTION_ADD_TO_CANDIDATES, ACTION_SKIP))
    else:  # worth_adding
        actions.append(ACTION_ADD_TO_CANDIDATES)
        if outfit.alternatives:
            # One condition, not two: V3-006 seeds alternatives only from
            # V3-005's `covers` items, so a non-empty list already implies the
            # user owns something in this category. Gating on the band as well
            # would be a second check that can never be false here — and a
            # branch no fixture can reach is a branch nobody can verify.
            actions.append(ACTION_STYLE_WITH_EXISTING)
        actions.append(ACTION_SKIP)
    return tuple(actions)


# --- Reasons -------------------------------------------------------------
# Each builder returns None when it has nothing evidenced to say. Order per
# decision state puts the reason that produced the judgment first (§5.3
# "ordered by importance"); the list is then truncated to MAX_REASONS.


def _redundancy_reason(
    locale: str, closet: ClosetEvidence, category: str | None, parsed: ParsedItem | None
) -> DecisionReason | None:
    label = _category_label(locale, category)
    count = _count_text(locale, closet.similar_item_count, floor=closet.same_category_scan_truncated)
    level = closet.redundancy_level
    if level == REDUNDANCY_UNKNOWN:
        return DecisionReason(REASON_REDUNDANCY, _text(locale, "reason.redundancy.unknown"))
    if level == REDUNDANCY_NONE:
        return DecisionReason(REASON_REDUNDANCY, _text(locale, "reason.redundancy.none", category=label))
    if level == REDUNDANCY_HIGH:
        return DecisionReason(
            REASON_REDUNDANCY,
            _text(locale, "reason.redundancy.high", count=count, category=label),
        )
    if level == REDUNDANCY_MEDIUM:
        return DecisionReason(
            REASON_REDUNDANCY,
            _text(
                locale,
                "reason.redundancy.medium",
                count=count,
                category=label,
                shared=_shared_attribute_label(locale, closet),
            ),
        )
    # LOW is assigned whenever no colour/material/scene match was ESTABLISHED —
    # including when the candidate attributes were simply not parsed. Asserting
    # "different colours and materials" is only honest when both attributes
    # were actually present to compare (codex review, BE#142).
    attributes_compared = parsed is not None and bool(parsed.colors) and bool(parsed.material_hints)
    key = "reason.redundancy.low" if attributes_compared else "reason.redundancy.low_unverified"
    return DecisionReason(
        REASON_REDUNDANCY,
        _text(locale, key, count=count, category=label),
    )


def _shared_attribute_label(locale: str, closet: ClosetEvidence) -> str:
    """Name the attribute that actually matched, rather than saying "similar".

    Reads the top `covers` entry's `match_reasons`, which is the same list
    V3-005 banded `medium` from — so the sentence cites the evidence that
    produced the band instead of paraphrasing it.
    """
    top = next((s for s in closet.similar_items if s.relation == RELATION_COVERS), None)
    reasons = top.match_reasons if top is not None else ()
    if REASON_SHARED_MATERIAL in reasons:
        return _text(locale, "shared.material")
    if REASON_SHARED_SCENE in reasons:
        return _text(locale, "shared.scene")
    return _text(locale, "shared.attributes")


def _compatibility_reason(locale: str, outfit: OutfitEvidence) -> DecisionReason | None:
    if outfit.status == OUTFIT_STATUS_UNKNOWN_CANDIDATE:
        return DecisionReason(REASON_COMPATIBILITY, _text(locale, "reason.compatibility.unknown"))
    if outfit.status == OUTFIT_STATUS_NO_AVAILABLE_ITEMS:
        return DecisionReason(REASON_COMPATIBILITY, _text(locale, "reason.compatibility.no_items"))
    if outfit.status != OUTFIT_STATUS_COMPOSED:
        return DecisionReason(REASON_COMPATIBILITY, _text(locale, "reason.compatibility.incomplete"))
    if _has_color_conflict(outfit):
        return DecisionReason(REASON_COMPATIBILITY, _text(locale, "reason.compatibility.color_conflict"))
    supporting = {item_id for o in outfit.outfits for item_id in o.item_ids}
    return DecisionReason(
        REASON_COMPATIBILITY,
        _text(
            locale,
            "reason.compatibility.composed",
            items=len(supporting),
            outfits=len(outfit.outfits),
        ),
    )


def _scene_reason(locale: str, closet: ClosetEvidence) -> DecisionReason | None:
    """The strongest scene statement we can evidence, or nothing.

    A covered scene beats an uncovered one because coverage is an observation
    ("you already wear something there") while absence is only the lack of a
    record — V3-006 makes the same call for the same reason.
    """
    if closet.coverage_scenarios:
        top = closet.coverage_scenarios[0]
        return DecisionReason(
            REASON_SCENE_COVERAGE,
            _text(
                locale,
                "reason.scene_coverage.covered",
                scene=_scene_label(locale, top.scene),
                count=len(top.covered_by_item_ids),
            ),
        )
    if closet.scenarios_without_evidence:
        return DecisionReason(
            REASON_SCENE_COVERAGE,
            _text(
                locale,
                "reason.scene_coverage.uncovered",
                scene=_scene_label(locale, closet.scenarios_without_evidence[0]),
            ),
        )
    return None


def _extra_purchase_reason(locale: str, outfit: OutfitEvidence) -> DecisionReason | None:
    if not outfit.missing_categories:
        return None
    joiner = _text(locale, "join")
    categories = joiner.join(_category_label(locale, c) for c in outfit.missing_categories)
    return DecisionReason(
        REASON_EXTRA_PURCHASE_NEEDED,
        _text(locale, "reason.extra_purchase_needed", categories=categories),
    )


def _reasons(
    locale: str,
    decision: str,
    parsed: ParsedItem | None,
    closet: ClosetEvidence,
    outfit: OutfitEvidence,
) -> tuple[DecisionReason, ...]:
    """2–3 reasons, the one that produced the judgment first."""
    category = parsed.category if parsed is not None else None
    redundancy = _redundancy_reason(locale, closet, category, parsed)
    compatibility = _compatibility_reason(locale, outfit)
    scene = _scene_reason(locale, closet)
    extra = _extra_purchase_reason(locale, outfit)

    if decision == PurchaseDecision.AVOID_DUPLICATE.value:
        ordered = [redundancy, scene, compatibility]
    elif decision == PurchaseDecision.WAIT.value:
        ordered = [extra, compatibility, redundancy]
    elif decision == PurchaseDecision.CONSIDER_ALTERNATE.value:
        # Lead with whichever signal produced the verdict: a clash is about the
        # colour on the hanger, a medium redundancy band is about the coat
        # already in the wardrobe.
        ordered = (
            [compatibility, redundancy, scene]
            if _has_color_conflict(outfit)
            else [redundancy, compatibility, scene]
        )
    elif decision == PurchaseDecision.INSUFFICIENT_INFORMATION.value:
        ordered = [redundancy, compatibility]
    else:  # worth_adding
        ordered = [compatibility, redundancy, scene, extra]

    reasons = tuple(r for r in ordered if r is not None)[:MAX_REASONS]
    if len(reasons) >= MIN_REASONS:
        return reasons
    # Every state above yields at least two evidenced statements today
    # (redundancy and compatibility both always render), so this is a guard
    # against a future evidence shape rather than a live padding path — and it
    # pads with evidence, never with filler.
    remaining = [r for r in (redundancy, compatibility, scene, extra) if r is not None and r not in reasons]
    return (reasons + tuple(remaining))[:MAX_REASONS]


def _summary(locale: str, closet: ClosetEvidence, outfit: OutfitEvidence, category: str | None) -> str:
    label = _category_label(locale, category)
    if closet.redundancy_level == REDUNDANCY_UNKNOWN:
        return _text(locale, "summary.unknown")
    if outfit.status == OUTFIT_STATUS_NO_AVAILABLE_ITEMS and not closet.similar_item_count:
        return _text(locale, "summary.no_items")
    if closet.redundancy_level == REDUNDANCY_NONE:
        return _text(locale, "summary.nothing_similar", category=label)
    count = _count_text(locale, closet.similar_item_count, floor=closet.same_category_scan_truncated)
    key = "summary.duplicate" if closet.redundancy_level == REDUNDANCY_HIGH else "summary.similar"
    return _text(locale, key, count=count, category=label)


def decide(
    parsed: ParsedItem | None,
    closet: ClosetEvidence,
    outfit: OutfitEvidence,
    *,
    concern: str = CONCERN_NONE,
    locale: str | None = None,
) -> PurchaseDecisionOutcome:
    """Turn V3-005 + V3-006 evidence into the bounded §5.3 decision.

    Never raises on degraded input: an unparsed candidate reaches
    `insufficient_information` through the evidence modules' own `unknown`
    states, which is why a `parse_failed` candidate is still decidable
    (contract §5.2) instead of a hard error.

    An unrecognized `concern` is treated as `none` rather than rejected here —
    request validation is the router's job, and a decision must not fail on a
    field that only grades the preview entry.
    """
    effective_locale = resolve_locale(locale) if locale else DEFAULT_LOCALE
    if effective_locale not in _TEMPLATES:
        effective_locale = DEFAULT_LOCALE
    normalized_concern = concern if concern in CONCERNS else CONCERN_NONE

    decision = _decision_state(closet, outfit)
    uncertainty = _uncertainty(closet, outfit)
    category = parsed.category if parsed is not None else None

    return PurchaseDecisionOutcome(
        decision=decision,
        decision_text=_text(effective_locale, f"decision.{decision}"),
        reasons=_reasons(effective_locale, decision, parsed, closet, outfit),
        closet_comparison=ClosetComparison(
            summary_text=_summary(effective_locale, closet, outfit, category),
            items=tuple(
                ComparisonItem(item_id=s.item_id, relation=s.relation)
                for s in closet.similar_items[:MAX_COMPARISON_ITEMS]
            ),
        ),
        uncertainty=uncertainty,
        visual_need=_visual_need(decision, parsed, outfit, uncertainty, normalized_concern),
        boundary_note=_text(effective_locale, "boundary_note"),
        next_actions=_next_actions(decision, closet, outfit),
    )


# Re-exported for the guards in the tests, which walk a rendered payload and
# assert every non-text leaf is one of these literals, an id, or a count.
VOCABULARIES: dict[str, frozenset[str]] = {
    "decision": frozenset(d.value for d in PurchaseDecision),
    "uncertainty": frozenset(u.value for u in DecisionUncertainty),
    "visual_need": frozenset(v.value for v in DecisionVisualNeed),
    "kind": REASON_KINDS,
    "next_actions": NEXT_ACTIONS,
    "relation": frozenset({RELATION_COVERS, "complements"}),
    "concern": CONCERNS,
}

__all__ = [
    "ACTION_ADD_TO_CANDIDATES",
    "ACTION_RECHECK_COLOR",
    "ACTION_SKIP",
    "ACTION_STYLE_WITH_EXISTING",
    "BANNED_CLAIM_TERMS",
    "CONCERNS",
    "CONCERN_COLOR",
    "CONCERN_NONE",
    "CONCERN_REDUNDANCY",
    "CONCERN_SCENE",
    "CONCERN_VOLUME",
    "DECISION_VERSION",
    "MAX_COMPARISON_ITEMS",
    "MAX_REASONS",
    "MIN_GROUNDING_ITEMS",
    "MIN_REASONS",
    "NEXT_ACTIONS",
    "REASON_ADOPTION_HISTORY",
    "REASON_COMPATIBILITY",
    "REASON_EXTRA_PURCHASE_NEEDED",
    "REASON_KINDS",
    "REASON_REDUNDANCY",
    "REASON_SCENE_COVERAGE",
    "VOCABULARIES",
    "VOLUME_SILHOUETTES",
    "ClosetComparison",
    "ComparisonItem",
    "DecisionReason",
    "PurchaseDecisionOutcome",
    "decide",
    "replay_decision",
]
