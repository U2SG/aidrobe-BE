"""Group album-scan candidates by physical garment.

Candidates are bucketed by (category, colour family); garments of a clearly
different type (tee vs shirt) never meet. Inside a bucket each candidate is
compared with every group's founder by a same-garment judge — the vision model
behind a verdict cache — and joins the first group it matches (union-find also
joins candidates already confirmed to the same wardrobe item). Only candidates
with a verified crop take part in visual merging; the rest stay on their own.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Protocol

from app.application.purchase.garment_type import garment_type

# Recognition names the same garment with neighbouring colour words across photos
# (white / cream, blue / light_blue). A colour belongs to exactly one family.
_COLOR_FAMILIES: dict[str, tuple[str, ...]] = {
    "blue": ("blue", "light_blue", "dark_blue", "navy", "denim", "sky_blue", "indigo"),
    "white": ("white", "off_white", "cream", "ivory"),
    "beige": ("beige", "khaki", "tan", "camel", "sand", "nude"),
    "gray": ("gray", "light_gray", "dark_gray", "charcoal", "silver", "heather_gray"),
    "green": ("green", "olive", "army_green", "dark_green", "sage"),
    "brown": ("brown", "dark_brown", "chocolate", "coffee"),
    "red": ("red", "maroon", "burgundy", "wine", "dark_red"),
    "pink": ("pink", "light_pink", "rose"),
    "yellow": ("yellow", "mustard"),
    "purple": ("purple", "lavender", "violet"),
}
_FAMILY_OF = {color: family for family, colors in _COLOR_FAMILIES.items() for color in colors}


def color_family(color: str | None) -> str:
    word = (color or "").strip().lower().replace("-", "_").replace(" ", "_").replace("grey", "gray")
    return _FAMILY_OF.get(word, word)


def garment_kind(canonical_name: str | None, title: str | None) -> str | None:
    """Coarse type, read from the English canonical name when there is one.

    Display titles add descriptive words (针织 / knit) that would type the same
    top differently across photos; the canonical name is the steadier signal.
    """
    if canonical_name:
        return garment_type(canonical_name.replace("_", " "))
    return garment_type(title)


def kinds_compatible(a: str | None, b: str | None) -> bool:
    return a is None or b is None or a == b


@dataclass(frozen=True)
class GarmentImage:
    key: str | None
    url: str | None

    @property
    def ref(self) -> str | None:
        return self.key or self.url


@dataclass(frozen=True)
class BatchCandidate:
    task_id: uuid.UUID
    temp_item_id: str
    category: str
    color_main: str
    title: str
    canonical_name: str | None
    # Verified garment crop (for a flat-lay photo: the photo itself); None = not usable for merging.
    image: GarmentImage | None
    confidence: float = 0.0
    partially_hidden: bool = False
    taken_at: date | None = None
    confirmed_item_id: uuid.UUID | None = None
    dismissed: bool = False

    @property
    def key(self) -> str:
        return f"{self.task_id}:{self.temp_item_id}"

    @property
    def mergeable(self) -> bool:
        return self.image is not None and self.image.ref is not None and not self.partially_hidden

    @property
    def bucket(self) -> tuple[str, str]:
        return self.category, color_family(self.color_main)

    @property
    def kind(self) -> str | None:
        return garment_kind(self.canonical_name, self.title)

    @property
    def label(self) -> str:
        return " ".join(part for part in (self.color_main, self.canonical_name or self.title) if part)


class SameGarmentJudge(Protocol):
    async def __call__(self, a: GarmentImage, b: GarmentImage, garment: str) -> bool | None:
        """True / False, or None when the comparison could not be made yet (budget, provider error)."""
        ...


class UnionFind:
    def __init__(self, keys: Iterable[str]) -> None:
        self._parent = {key: key for key in keys}

    def find(self, key: str) -> str:
        root = key
        while self._parent[root] != root:
            root = self._parent[root]
        while self._parent[key] != root:
            self._parent[key], key = root, self._parent[key]
        return root

    def union(self, a: str, b: str) -> None:
        root_a, root_b = self.find(a), self.find(b)
        if root_a != root_b:
            # The first argument's root stays the representative.
            self._parent[root_b] = root_a


@dataclass(frozen=True)
class Grouping:
    groups: list[list[BatchCandidate]]
    # Some comparison is still outstanding; groups may still merge on a later read.
    pending: bool


async def group_candidates(candidates: list[BatchCandidate], judge: SameGarmentJudge) -> Grouping:
    """Group `candidates` (in stable order) by garment. Buckets are judged concurrently."""
    uf = UnionFind(candidate.key for candidate in candidates)
    by_item: dict[uuid.UUID, str] = {}
    for candidate in candidates:
        if candidate.confirmed_item_id is None:
            continue
        first = by_item.setdefault(candidate.confirmed_item_id, candidate.key)
        uf.union(first, candidate.key)

    buckets: dict[tuple[str, str], list[BatchCandidate]] = {}
    for candidate in candidates:
        if candidate.mergeable:
            buckets.setdefault(candidate.bucket, []).append(candidate)

    async def merge_bucket(members: list[BatchCandidate]) -> bool:
        pending = False
        founders: list[BatchCandidate] = []
        tasks_of: dict[str, set[uuid.UUID]] = {}
        latest: dict[str, BatchCandidate] = {}

        async def against_group(candidate: BatchCandidate, founder: BatchCandidate) -> bool | None:
            # Founder first; on a "no", one retry against the group's latest member,
            # since a small or dark founder crop hides the details that decide.
            verdict = await judge(_image(candidate), _image(founder), candidate.label)
            other = latest.get(founder.key, founder)
            if verdict is False and other is not founder:
                verdict = await judge(_image(candidate), _image(other), candidate.label)
            return verdict

        for candidate in members:
            root = uf.find(candidate.key)
            founder = next((f for f in founders if uf.find(f.key) == root), None)
            if founder is not None:
                tasks_of[founder.key].add(candidate.task_id)
                latest[founder.key] = candidate
                continue
            options = [
                f for f in founders
                # Two garments of one photo are never the same item.
                if candidate.task_id not in tasks_of[f.key] and kinds_compatible(f.kind, candidate.kind)
            ]
            verdicts = await asyncio.gather(*(against_group(candidate, f) for f in options))
            pending = pending or any(verdict is None for verdict in verdicts)
            matches = [f for f, verdict in zip(options, verdicts, strict=True) if verdict]
            if not matches:
                founders.append(candidate)
                tasks_of[candidate.key] = {candidate.task_id}
                continue
            # A candidate matching several groups bridges them: the founders'
            # own comparison was a miss (one side's crop was the weaker view).
            target = matches[0]
            uf.union(target.key, candidate.key)
            tasks_of[target.key].add(candidate.task_id)
            latest[target.key] = candidate
            for other in matches[1:]:
                if tasks_of[other.key] & tasks_of[target.key]:
                    continue  # would put two garments of one photo together
                uf.union(target.key, other.key)
                tasks_of[target.key] |= tasks_of.pop(other.key)
                founders.remove(other)
        return pending

    pending = any(await asyncio.gather(*(merge_bucket(members) for members in buckets.values())))
    grouped: dict[str, list[BatchCandidate]] = {}
    for candidate in candidates:
        grouped.setdefault(uf.find(candidate.key), []).append(candidate)
    return Grouping(groups=list(grouped.values()), pending=pending)


def _image(candidate: BatchCandidate) -> GarmentImage:
    assert candidate.image is not None
    return candidate.image


def best_member(members: list[BatchCandidate]) -> BatchCandidate:
    """The member whose crop best represents the group: mergeable first, then most confident."""
    ranked = sorted(
        enumerate(members), key=lambda pair: (not pair[1].mergeable, -pair[1].confidence, pair[0]),
    )
    return ranked[0][1]
