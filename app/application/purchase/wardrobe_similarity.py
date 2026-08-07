"""V3-005 — wardrobe similarity and redundancy evidence (pure layer).

No DB, no IO, no SQLAlchemy import: everything here is a function of a
`ParsedItem` (V3-004) plus a list of `OwnedItemFacts` projections. The DB half
lives in `compute_similarity_evidence.py`, which does the queries and builds
the projections. That split is the `candidate_normalizer` / `ingest_candidate`
arrangement V3-004 established, and it is what lets the deterministic-fixture
tests run without a container.

**Why there is no score in the payload.** V3-005 acceptance 3 forbids "a
misleading universal suitability percentage" and V3_SPEC §5.4 spells out the
anti-goal ("避免虚假的精确评分，如'适合度 87 分'"). So the emitted evidence is
categorical plus *counts*: a `redundancy_level` band, a `relation` per item,
the reasons that produced it, and how many owned items cover each scene. A
score IS computed here — `_match_score` — but only to order a list, and it
never reaches the payload. Rank is carried by list order, nothing else.
Counting overlapping use-cases is not false precision; scoring them would be.

**Field names come from the two authorities, verbatim.** V3_SPEC §10.3 names
the closet evidence block (`similar_item_count`, `compatible_item_ids`,
`coverage_scenarios`); `docs/v3-p0-contract.md` §5.3 freezes what V3-007/V3-008
surface (`closet_comparison.similar_items[].{item_id, relation}` with
`relation` ∈ `covers|complements`). Emitting those literals means V3-007
renames nothing, and it makes acceptance 3 structural rather than a promise:
the frozen per-item shape has no numeric field to put a percentage in.

**The scene vocabulary bridge is real, not assumed.** `WardrobeItem` has no
scene column, so "overlapping use-cases" has to be evidenced from something
that exists. Three sources, strongest first:

1. `fit_feedback.scene` — the user rated this very item in that scene. Its
   vocabulary is `FitScene`, which is a verified subset of the candidate-side
   `candidate_normalizer.SCENE_TAGS` (see `SHARED_SCENES`, computed as a real
   intersection so it cannot drift into a lie).
2. `wardrobe_items.style_tags` — free-text style descriptors that sometimes
   carry a scene word (`formal`, `casual`). This is the existing
   `wardrobe_relationship_repo._rule_based_relationship` "occasion_overlap"
   signal, reused rather than reinvented.
3. Same category and nothing else — the deterministic default. Weak, and
   labelled as such so V3-007 can down-weight it.

Every coverage entry carries the `source` that produced it. `formality_score`
was considered as a fourth signal and rejected: the write schema constrains it
to 1..5 while `outfits/scoring/static.py` reads it as 0..100, so its scale is
ambiguous in this codebase and V3-005 is not the place to resolve that.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any

from app.application.purchase.candidate_normalizer import SCENE_TAGS, ParsedItem
from app.common.enums import Category, FitScene

# Bump when the emitted payload shape changes. Snapshots persisted by V3-007
# carry it so a replay knows which reader to use (the
# `DECISION_SNAPSHOT_VERSION` convention from V3-003).
EVIDENCE_VERSION = 1

# --- Frozen literals (docs/v3-p0-contract.md §5.3) -----------------------
# `relation` is frozen by the contract; V3-007/V3-008 pass these through
# untouched. Kept as module constants rather than a `app/common/enums.py`
# member because nothing here is persisted as a column — there is no CHECK
# constraint for them to stay in sync with.
RELATION_COVERS = "covers"
RELATION_COMPLEMENTS = "complements"

# Redundancy bands. `none` = we looked and found nothing similar; `unknown` =
# we could not look, because the candidate never parsed a category. V3-007
# needs those to be distinguishable to reach `insufficient_information`
# honestly instead of reporting a confident "nothing similar".
REDUNDANCY_HIGH = "high"
REDUNDANCY_MEDIUM = "medium"
REDUNDANCY_LOW = "low"
REDUNDANCY_NONE = "none"
REDUNDANCY_UNKNOWN = "unknown"

# Why a given owned item was called similar. Surfaced so the reason text
# V3-007 writes can cite the actual attribute instead of a number.
REASON_SAME_CATEGORY = "same_category"
REASON_SAME_COLOR = "same_color"
REASON_SHARED_MATERIAL = "shared_material"
REASON_SHARED_SCENE = "shared_scene"

# Provenance of a scene-coverage claim, strongest first.
SCENE_SOURCE_WEAR_FEEDBACK = "wear_feedback"
SCENE_SOURCE_ITEM_TAGS = "item_tags"
SCENE_SOURCE_CATEGORY_DEFAULT = "category_default"
_SCENE_SOURCE_ORDER = (
    SCENE_SOURCE_WEAR_FEEDBACK,
    SCENE_SOURCE_ITEM_TAGS,
    SCENE_SOURCE_CATEGORY_DEFAULT,
)

# The scenes both sides of the comparison can actually speak about: the owned
# side persists `FitScene` on `fit_feedback.scene`, the candidate side emits
# `candidate_normalizer.SCENE_TAGS`. Computed as a genuine intersection so it
# can never claim a bridge that does not exist — `test_v3_wardrobe_similarity`
# asserts it is non-empty and equal to the `FitScene` members, which fails
# loudly if either vocabulary is edited apart from the other.
SHARED_SCENES: frozenset[str] = frozenset(s.value for s in FitScene) & frozenset(SCENE_TAGS)

# Categories that pair with the candidate rather than replace it. Deliberately
# a copy of `shopping/sense_check._COMPLEMENTS` rather than an import: that is
# a private name inside a live M17 endpoint module which V3-005 must not
# couple itself to, and the two are free to diverge (this one keys off
# `ParsedItem`, M17's off `VisionResult`).
COMPLEMENTARY_CATEGORIES: dict[str, tuple[str, ...]] = {
    Category.TOP.value: (Category.BOTTOM.value, Category.SHOES.value, Category.OUTERWEAR.value),
    Category.BOTTOM.value: (Category.TOP.value, Category.SHOES.value, Category.OUTERWEAR.value),
    Category.OUTERWEAR.value: (Category.TOP.value, Category.BOTTOM.value, Category.SHOES.value),
    Category.DRESS.value: (Category.SHOES.value, Category.OUTERWEAR.value),
    Category.SHOES.value: (Category.TOP.value, Category.BOTTOM.value, Category.DRESS.value),
}

# Ranking weights — internal only. See the module docstring: these order a
# list and are never emitted.
_WEIGHT_SAME_COLOR = 4
_WEIGHT_SHARED_MATERIAL = 2
_WEIGHT_SHARED_SCENE = 1

# Cap the emitted lists. A 400-item wardrobe must not produce a 400-entry
# snapshot, and the decision page shows a handful (V3_SPEC §5.4 layer 3).
MAX_SIMILAR_ITEMS = 5
MAX_COMPATIBLE_ITEMS = 10


@dataclass(frozen=True)
class OwnedItemFacts:
    """The projection of a `WardrobeItem` that similarity actually reads.

    A plain value object on purpose: it keeps this module free of SQLAlchemy so
    the deterministic-fixture tests need no database, and it makes the set of
    columns V3-005 depends on explicit and small.
    """

    item_id: uuid.UUID
    category: str
    color_main: str = ""
    material: str | None = None
    # Normalized `wardrobe_items.style_tags` (JSONB is `{"tags": [...]}` in
    # most writers and a bare list in others — `normalize_style_tags` folds both).
    style_tags: tuple[str, ...] = ()
    # Distinct `fit_feedback.scene` values recorded against this item.
    wear_scenes: tuple[str, ...] = ()


@dataclass(frozen=True)
class SimilarItemEvidence:
    """One row of contract §5.3 `closet_comparison.similar_items`."""

    item_id: uuid.UUID
    relation: str
    match_reasons: tuple[str, ...]

    def as_payload(self) -> dict[str, Any]:
        # `image_url` is contract §5.3's third key and is deliberately absent:
        # it is a signed URL with a 30-minute TTL, so it is materialized at
        # read time by the endpoint (V3-008), never frozen into a snapshot.
        return {
            "item_id": str(self.item_id),
            "relation": self.relation,
            "match_reasons": list(self.match_reasons),
        }


@dataclass(frozen=True)
class SceneCoverage:
    """One entry of V3_SPEC §10.3 `coverage_scenarios`.

    `covered_by_item_ids` holds only the items at the strongest available
    `source`. Mixing a real wear record with a category default in one list
    would make the strong evidence unreadable.
    """

    scene: str
    covered_by_item_ids: tuple[uuid.UUID, ...]
    source: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "scene": self.scene,
            "covered_by_item_ids": [str(i) for i in self.covered_by_item_ids],
            "covered_by_item_count": len(self.covered_by_item_ids),
            "source": self.source,
        }


@dataclass(frozen=True)
class ClosetEvidence:
    """V3_SPEC §10.3 `closet_evidence`, plus the §5.3 per-item relation."""

    redundancy_level: str
    similar_items: tuple[SimilarItemEvidence, ...] = ()
    coverage_scenarios: tuple[SceneCoverage, ...] = ()
    scenarios_without_evidence: tuple[str, ...] = ()
    evidence_version: int = EVIDENCE_VERSION

    @property
    def similar_item_count(self) -> int:
        """§10.3 `similar_item_count` — items that would do the same job."""
        return sum(1 for s in self.similar_items if s.relation == RELATION_COVERS)

    @property
    def compatible_item_ids(self) -> list[uuid.UUID]:
        """§10.3 `compatible_item_ids` — owned items the candidate pairs with."""
        return [s.item_id for s in self.similar_items if s.relation == RELATION_COMPLEMENTS]

    def as_payload(self) -> dict[str, Any]:
        return {
            "evidence_version": self.evidence_version,
            "redundancy_level": self.redundancy_level,
            "similar_item_count": self.similar_item_count,
            "similar_items": [s.as_payload() for s in self.similar_items],
            "compatible_item_ids": [str(i) for i in self.compatible_item_ids],
            "coverage_scenarios": [c.as_payload() for c in self.coverage_scenarios],
            "scenarios_without_evidence": list(self.scenarios_without_evidence),
        }


def normalize_style_tags(raw: Any) -> tuple[str, ...]:
    """`wardrobe_items.style_tags` → lowercase tags.

    The column is written as `{"tags": [...]}` by the style repo and the
    style-learn worker but as a bare list elsewhere; `style_context.py` already
    tolerates both and this mirrors it rather than picking a winner.
    """
    tags = raw.get("tags") if isinstance(raw, dict) else raw
    if not isinstance(tags, list):
        return ()
    return tuple(t.strip().lower() for t in tags if isinstance(t, str) and t.strip())


def _candidate_colors(parsed: ParsedItem) -> set[str]:
    return {c.strip().lower() for c in (parsed.colors or []) if isinstance(c, str) and c.strip()}


def _candidate_scenes(parsed: ParsedItem) -> list[str]:
    """Candidate scenes, de-duplicated, order preserved for a stable payload."""
    seen: list[str] = []
    for scene in parsed.scene_tags or []:
        normalized = scene.strip().lower() if isinstance(scene, str) else ""
        if normalized and normalized not in seen:
            seen.append(normalized)
    return seen


def match_reasons(parsed: ParsedItem, facts: OwnedItemFacts) -> tuple[str, ...]:
    """Which structured attributes this owned item shares with the candidate.

    Only called for same-category items — `REASON_SAME_CATEGORY` is therefore
    always present, and is emitted explicitly so the payload reads on its own.
    """
    reasons = [REASON_SAME_CATEGORY]
    if facts.color_main and facts.color_main.strip().lower() in _candidate_colors(parsed):
        reasons.append(REASON_SAME_COLOR)
    materials = {m.strip().lower() for m in (parsed.material_hints or []) if isinstance(m, str)}
    if facts.material and facts.material.strip().lower() in materials:
        reasons.append(REASON_SHARED_MATERIAL)
    if set(_candidate_scenes(parsed)) & (set(facts.wear_scenes) | set(facts.style_tags)):
        reasons.append(REASON_SHARED_SCENE)
    return tuple(reasons)


def _match_score(reasons: tuple[str, ...]) -> int:
    """Internal ranking weight. Never emitted — see the module docstring."""
    return (
        _WEIGHT_SAME_COLOR * (REASON_SAME_COLOR in reasons)
        + _WEIGHT_SHARED_MATERIAL * (REASON_SHARED_MATERIAL in reasons)
        + _WEIGHT_SHARED_SCENE * (REASON_SHARED_SCENE in reasons)
    )


def rank_by_attributes(parsed: ParsedItem, facts: list[OwnedItemFacts]) -> list[OwnedItemFacts]:
    """Deterministic structured-attribute ordering, strongest match first.

    This is the fallback tier: it is what runs when pgvector is off, when no
    embedding anchor exists, or when the vector query returns nothing. Ties
    break on `item_id` so the same wardrobe always yields the same order —
    without that, snapshots would differ run to run and the fixtures below
    could not assert an order at all.
    """
    return sorted(facts, key=lambda f: (-_match_score(match_reasons(parsed, f)), str(f.item_id)))


def _scene_source(scene: str, facts: OwnedItemFacts) -> str | None:
    """Strongest evidence that this owned item serves `scene`, or None.

    The cascade is the module docstring's three sources. The last tier applies
    only inside `SHARED_SCENES`: outside it the owned side has no comparable
    vocabulary, so claiming coverage from category alone would be an invention.
    """
    if scene in facts.wear_scenes:
        return SCENE_SOURCE_WEAR_FEEDBACK
    if scene in facts.style_tags:
        return SCENE_SOURCE_ITEM_TAGS
    if scene in SHARED_SCENES:
        return SCENE_SOURCE_CATEGORY_DEFAULT
    return None


def _coverage(scene: str, covers: list[OwnedItemFacts]) -> SceneCoverage | None:
    """Which owned items cover `scene`, reported at the strongest tier only."""
    by_source: dict[str, list[uuid.UUID]] = {}
    for facts in covers:
        source = _scene_source(scene, facts)
        if source is not None:
            by_source.setdefault(source, []).append(facts.item_id)
    for source in _SCENE_SOURCE_ORDER:
        if by_source.get(source):
            return SceneCoverage(
                scene=scene,
                covered_by_item_ids=tuple(by_source[source]),
                source=source,
            )
    return None


def _redundancy_level(top_reasons: tuple[str, ...] | None) -> str:
    """Band the strongest match. Bands, not a score — acceptance 3."""
    if top_reasons is None:
        return REDUNDANCY_NONE
    if REASON_SAME_COLOR in top_reasons:
        return REDUNDANCY_HIGH
    if REASON_SHARED_MATERIAL in top_reasons or REASON_SHARED_SCENE in top_reasons:
        return REDUNDANCY_MEDIUM
    return REDUNDANCY_LOW


def unknown_evidence() -> ClosetEvidence:
    """The honest answer when the candidate never parsed a category.

    Distinct from an empty result: `none` asserts we searched the wardrobe and
    found nothing comparable, `unknown` says we could not search at all. V3-007
    maps this one to `insufficient_information`.
    """
    return ClosetEvidence(redundancy_level=REDUNDANCY_UNKNOWN)


def build_evidence(parsed: ParsedItem | None, ranked: list[OwnedItemFacts]) -> ClosetEvidence:
    """Assemble the evidence from an already-ordered list of owned items.

    `ranked` order is authoritative and is preserved into the payload — that is
    how the pgvector tier gets to matter without this module knowing embeddings
    exist. The DB layer ranks (vector when available, `rank_by_attributes`
    otherwise) and hands the result here.

    Never raises on partial input: a `ParsedItem` with no category is the
    expected outcome of a degraded V3-004 parse, and it must degrade to
    `unknown_evidence()` rather than break the decision flow.
    """
    if parsed is None or not parsed.is_usable:
        return unknown_evidence()

    category = (parsed.category or "").strip().lower()
    complements = COMPLEMENTARY_CATEGORIES.get(category, ())
    covers = [f for f in ranked if f.category == category]
    complementary = [f for f in ranked if f.category != category and f.category in complements]

    similar: list[SimilarItemEvidence] = [
        SimilarItemEvidence(
            item_id=f.item_id,
            relation=RELATION_COVERS,
            match_reasons=match_reasons(parsed, f),
        )
        for f in covers[:MAX_SIMILAR_ITEMS]
    ]
    top_reasons = similar[0].match_reasons if similar else None
    similar.extend(
        SimilarItemEvidence(
            item_id=f.item_id,
            relation=RELATION_COMPLEMENTS,
            match_reasons=(),
        )
        for f in complementary[:MAX_COMPATIBLE_ITEMS]
    )

    covered: list[SceneCoverage] = []
    uncovered: list[str] = []
    for scene in _candidate_scenes(parsed):
        # Coverage is judged against same-category items only: a complementary
        # item pairs with the candidate, it does not do the candidate's job.
        entry = _coverage(scene, covers)
        if entry is None:
            uncovered.append(scene)
        else:
            covered.append(entry)

    return ClosetEvidence(
        redundancy_level=_redundancy_level(top_reasons),
        similar_items=tuple(similar),
        coverage_scenarios=tuple(covered),
        scenarios_without_evidence=tuple(uncovered),
    )


_ALL_MATCH_REASONS = frozenset(
    {REASON_SAME_CATEGORY, REASON_SAME_COLOR, REASON_SHARED_MATERIAL, REASON_SHARED_SCENE}
)
_ALL_RELATIONS = frozenset({RELATION_COVERS, RELATION_COMPLEMENTS})
_ALL_REDUNDANCY_LEVELS = frozenset(
    {REDUNDANCY_HIGH, REDUNDANCY_MEDIUM, REDUNDANCY_LOW, REDUNDANCY_NONE, REDUNDANCY_UNKNOWN}
)
# Re-exported for the acceptance-3 guard in the tests, which walks a rendered
# payload and asserts every leaf is one of these literals, an id, or a count.
VOCABULARIES: dict[str, frozenset[str]] = {
    "match_reasons": _ALL_MATCH_REASONS,
    "relation": _ALL_RELATIONS,
    "redundancy_level": _ALL_REDUNDANCY_LEVELS,
    "scene_source": frozenset(_SCENE_SOURCE_ORDER),
}

__all__ = [
    "COMPLEMENTARY_CATEGORIES",
    "EVIDENCE_VERSION",
    "MAX_COMPATIBLE_ITEMS",
    "MAX_SIMILAR_ITEMS",
    "REASON_SAME_CATEGORY",
    "REASON_SAME_COLOR",
    "REASON_SHARED_MATERIAL",
    "REASON_SHARED_SCENE",
    "REDUNDANCY_HIGH",
    "REDUNDANCY_LOW",
    "REDUNDANCY_MEDIUM",
    "REDUNDANCY_NONE",
    "REDUNDANCY_UNKNOWN",
    "RELATION_COMPLEMENTS",
    "RELATION_COVERS",
    "SCENE_SOURCE_CATEGORY_DEFAULT",
    "SCENE_SOURCE_ITEM_TAGS",
    "SCENE_SOURCE_WEAR_FEEDBACK",
    "SHARED_SCENES",
    "VOCABULARIES",
    "ClosetEvidence",
    "OwnedItemFacts",
    "SceneCoverage",
    "SimilarItemEvidence",
    "build_evidence",
    "match_reasons",
    "normalize_style_tags",
    "rank_by_attributes",
    "unknown_evidence",
]
